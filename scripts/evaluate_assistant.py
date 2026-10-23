"""Export CSV results while running a question bank through the real read-only assistant, with checkpoints.

Reference answers are retained for comparison but never sent to the assistant.
Numeric checks are diagnostics, not semantic accuracy scores.
"""
import argparse
import ast
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
NUMBERS = re.compile(r'(?<![A-Za-z\d])\d+(?:,\d{3})*(?:\.\d+)?')
TERMINAL = {'completed', 'failed', 'cancelled'}


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def load_questions(path):
    """Read data-only dict(...) entries without executing a supplied Python file."""
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    node = next(n.value for n in tree.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'QA' for t in n.targets))
    if not isinstance(node, ast.List):
        raise ValueError('QA must be a data list')
    items = []
    for entry in node.elts:
        if not isinstance(entry, ast.Call) or not isinstance(entry.func, ast.Name) or entry.func.id != 'dict' or entry.args:
            raise ValueError('Only literal dict entries are accepted')
        item = {k.arg: ast.literal_eval(k.value) for k in entry.keywords if k.arg}
        if set(item) != {'no', 'module', 'q', 'a', 'src', 'quote', 'note'}:
            raise ValueError('Unexpected question fields')
        items.append(item)
    if len(items) != 100 or sorted(i['no'] for i in items) != list(range(1, 101)):
        raise ValueError('Expected exactly 100 uniquely numbered questions')
    return sorted(items, key=lambda i: i['no'])


def numeric_tokens(text):
    text = unicodedata.normalize('NFKC', text)
    return sorted({str(Decimal(m.group().replace(',', '')).normalize()) for m in NUMBERS.finditer(text)})


def compare(item, answer):
    body = answer.split('原文定位：', 1)[0]
    body = re.sub(r'\[M\d+\]|[a-z][a-z0-9_-]+#(?:block-\d+-)?chars-\d+-\d+', '', body)
    expected, actual = set(numeric_tokens(item['a'])), set(numeric_tokens(body))
    missing = sorted(expected - actual)
    quote_numbers = set(numeric_tokens(item['quote']))
    unsupported_reference = sorted(expected - quote_numbers)
    return {'expected_numbers': sorted(expected), 'matched_numbers': sorted(expected & actual),
        'missing_numbers': missing, 'numeric_check': ('无阿拉伯数字题' if not expected else
            '参考数字均出现' if not missing else '参考数字部分出现' if expected & actual else '参考数字未出现'),
        'reference_warning': ('参考答案数字与所给摘录未完全对应：' + '、'.join(unsupported_reference)) if unsupported_reference else '',
        'manual_verdict': '待核对', 'manual_note': '',
        'comparison_note': '数字出现不代表口径、单位、对象或语义正确；参考答案也需核对原文。'}


class API:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip('/')

    def request(self, path, payload=None):
        raw = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=raw,
            headers={'Content-Type': 'application/json', 'X-KB-Request': '1'},
            method='POST' if raw is not None else 'GET')
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)


def assistant_payload(item, industry, conversation):
    return {'prompt': item['q'], 'industry': industry, 'research': False,
        'conversation': conversation, 'allow_source_changes': False}


def snapshot(api, industry, material_id):
    state = api.request('/api/state')
    cfg = state['agent']
    if cfg['base_url'].rstrip('/') != 'http://127.0.0.1:8091/v1' or cfg['model'] != 'qwen3.8-27b':
        raise ValueError('本轮只测试本机Qwen；当前配置已变化，未启动外部模型请求')
    material = api.request('/api/materials/' + industry + '/' + material_id)
    return {'provider': {k: cfg[k] for k in ('provider_id', 'base_url', 'protocol', 'model')},
        'fingerprint': (material.get('extraction_job') or {}).get('fingerprint'),
        'index_version': (material.get('extraction_job') or {}).get('version'),
        'material_verification': material.get('verification_status')}


def citations(answer):
    return sorted(set(re.findall(r'[a-z][a-z0-9_-]+#(?:block-\d+-)?chars-\d+-\d+', answer)))


def validate_citations(answer, industry):
    sys.path.insert(0, str(ROOT))
    from web.service import Store, kb
    from web.knowledge import Knowledge
    store, checked = Store(ROOT), []
    for citation in citations(answer):
        key = citation.split('#', 1)[0]
        try:
            material = Knowledge(store).get(industry, key)
            m = re.fullmatch(re.escape(key) + r'#(?:block-(\d+)-)?chars-(\d+)-(\d+)', citation)
            block, first, last = m.groups()
            if block:
                parts = {n+1: p['text'] for n, p in Knowledge(store).index_parts(material)}
                body = parts.get(int(block), '')
            else:
                body = material.get('content') or material.get('summary', '')
            quote = body[int(first)-1:int(last)]
            valid = bool(quote) and kb.material_quote_matches(store.home(industry), material,
                {'citation': citation, 'quote': quote})
            checked.append({'citation': citation, 'valid': valid})
        except (ValueError, OSError, KeyError):
            checked.append({'citation': citation, 'valid': False})
    return {'citations': checked, 'citation_check': '无原文定位' if not checked else
        '定位有效' if all(x['valid'] for x in checked) else '含无效定位'}


def export_state(run, folder, workbook, node, renderer, render=False):
    counts = Counter(item['status'] for item in run['items'])
    run.update(updated_at=now(), counts=dict(counts))
    atomic_json(folder / 'run.json', run)
    csv_path = workbook.with_suffix('.csv')
    temp = csv_path.with_suffix('.csv.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['编号', '专题', '问题', '实际回答', '参考答案', '状态', '耗时(秒)', '原文引用',
            '定位检查', '参考数字检查', '未出现数字', '参考答案提示', '人工结论', '核对备注', '参考出处', '系统错误'])
        for item in run['items']:
            values = [item['no'], item['module'], item['q'], item.get('answer', ''), item['a'], item['status'],
                item.get('seconds'), '\n'.join(x['citation'] for x in item.get('citations', [])),
                item.get('citation_check', ''), item.get('numeric_check', ''), '、'.join(item.get('missing_numbers', [])),
                item.get('reference_warning', ''), item.get('manual_verdict', '待核对'), item.get('manual_note', ''),
                item['src'], item.get('error', '')]
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for v in values])
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, csv_path)


def process_alive(pid):
    if sys.platform == 'win32':
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--questions', type=Path, default=ROOT / 'work/evaluation/questions.py')
    parser.add_argument('--run-dir', type=Path, default=ROOT / 'work/evaluation/assistant-100')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/evaluation/results.csv')
    parser.add_argument('--base-url', default='http://127.0.0.1:8766')
    parser.add_argument('--industry')
    parser.add_argument('--material', help='Material ID whose version is frozen for this run')
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.set_defaults(node=None)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('--limit must be between 1 and 100')
    folder, workbook = args.run_dir.resolve(), args.output.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    workbook.parent.mkdir(parents=True, exist_ok=True)
    path, stop_path, lock = folder / 'run.json', folder / 'stop.request', folder / 'runner.lock'
    if args.stop:
        stop_path.write_text(now(), encoding='utf-8')
        return
    if args.status:
        run = json.loads(path.read_text(encoding='utf-8'))
        print(json.dumps({k: run.get(k) for k in ('status', 'counts', 'updated_at', 'pause_reason')}, ensure_ascii=False))
        return
    if not args.industry or not args.material:
        parser.error('--industry and --material are required to start or resume')
    items = load_questions(args.questions)
    digest = hashlib.sha256(args.questions.read_bytes()).hexdigest()
    if lock.exists():
        prior = json.loads(lock.read_text(encoding='utf-8'))
        if process_alive(prior['pid']):
            raise ValueError('此100题测试已在运行，不能重复启动')
        lock.unlink()
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump({'pid': os.getpid(), 'started_at': now()}, handle)
    run, current_job = None, None
    api = API(args.base_url)
    renderer = folder / 'build_workbook.mjs'
    try:
        if path.exists():
            run = json.loads(path.read_text(encoding='utf-8'))
            if run['question_sha256'] != digest:
                raise ValueError('题库版本变化，请建立新的测试目录')
        else:
            run = {'schema_version': 1, 'id': uuid4().hex, 'created_at': now(), 'status': '准备中',
                'question_sha256': digest, 'question_source': str(args.questions.resolve()), 'industry': args.industry,
                'items': [{**item, 'status': '待测试', 'attempts': [], 'manual_verdict': '待核对'} for item in items]}
        run['snapshot'] = run.get('snapshot') or snapshot(api, args.industry, args.material)
        run['status'] = '待启动' if args.prepare_only else '运行中'
        run.pop('pause_reason', None)
        stop_path.unlink(missing_ok=True)
        export_state(run, folder, workbook, args.node, renderer, render=True)
        if args.prepare_only:
            return
        failures, attempted = 0, 0
        for item in run['items']:
            if item['status'] in ('已回答', '系统失败'):
                continue
            if attempted >= args.limit or stop_path.exists():
                run.update(status='已暂停', pause_reason='达到本轮题数上限或收到停止请求')
                break
            if snapshot(api, args.industry, args.material) != run['snapshot']:
                run.update(status='已暂停', pause_reason='模型配置或资料版本变化，停止混合不同版本结果')
                break
            current_job = item.get('job_id') if item['status'] in ('运行中', '待重试') else None
            item.update(status='运行中', started_at=now())
            started = time.monotonic()
            try:
                if current_job:
                    try:
                        prior_job = api.request('/api/agent/jobs/' + current_job)
                        if prior_job['status'] == 'cancelled':
                            current_job = None
                    except urllib.error.HTTPError as exc:
                        if exc.code != 404:
                            raise
                        current_job = None
                if not current_job:
                    conversation = 'qa-' + run['id'][:12] + '-' + str(item['no']) + '-' + uuid4().hex[:8]
                    created = api.request('/api/agent/jobs', assistant_payload(item, args.industry, conversation))
                    current_job = created['id']
                    item.update(job_id=current_job, conversation=created['conversation'])
                    item['attempts'].append({'job_id': current_job, 'at': now()})
                export_state(run, folder, workbook, args.node, renderer)
                while True:
                    job = api.request('/api/agent/jobs/' + current_job)
                    if job['status'] in TERMINAL:
                        break
                    if stop_path.exists() or time.monotonic()-started > 420:
                        api.request('/api/agent/jobs/' + current_job + '/stop', {})
                        raise TimeoutError('停止或超时')
                    time.sleep(2)
                atomic_json(folder / 'answers' / f'{item["no"]:03d}.json', job)
                answer = next((e.get('text', '') for e in reversed(job['events']) if e['type'] == 'answer'), '')
                if job['status'] != 'completed' or not answer:
                    errors = [e.get('detail') or e.get('title') for e in job['events'] if e['type'] == 'error']
                    item.update(status='系统失败', error='；'.join(errors) or '任务未完成或没有回答')
                    failures += 1
                else:
                    event = next(e for e in reversed(job['events']) if e['type'] == 'answer')
                    item.pop('error', None)
                    item.update(status='已回答', answer=answer, requests=event.get('requests'), tokens=event.get('tokens'),
                        **compare(item, answer), **validate_citations(answer, args.industry))
                    failures = 0
                item.update(finished_at=now(), seconds=round(time.monotonic()-started, 2))
                attempted += 1
                current_job = None
                export_state(run, folder, workbook, args.node, renderer)
                print(json.dumps({'no': item['no'], 'status': item['status'], 'seconds': item['seconds'],
                    'counts': run['counts']}, ensure_ascii=False), flush=True)
                if failures >= 3:
                    run.update(status='已暂停', pause_reason='连续3题系统失败，保留余题待测，不扫描成100题内容错误')
                    break
            except (OSError, TimeoutError, ValueError) as exc:
                item.update(status='待重试', error=type(exc).__name__ + '：服务中断、停止或配置变化，未计回答错误',
                    seconds=round(time.monotonic()-started, 2))
                run.update(status='已暂停', pause_reason=item['error'])
                break
        else:
            run.update(status='已完成', finished_at=now())
        export_state(run, folder, workbook, args.node, renderer, render=True)
    except Exception as exc:
        if run:
            run.update(status='已暂停', pause_reason=type(exc).__name__ + '：测试环境尚未就绪，原结果保留')
            export_state(run, folder, workbook, args.node, renderer)
        raise
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
