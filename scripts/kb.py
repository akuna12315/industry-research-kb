"""Local, dependency-free evidence store and Chinese-capable document search."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
STATUSES = {'confirmed', 'reported', 'planned', 'needs_verification', 'superseded'}
LABELS = {'confirmed': '已核验', 'reported': '来源声称', 'planned': '计划/目标',
          'needs_verification': '待核验', 'superseded': '已被更新替代'}


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temp.write_text(content, encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


@contextmanager
def lock(root):
    path = root / 'data' / 'write.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError('另一轮更新正在进行。若上次意外中断，请先核查 data/write.lock 中的进程。')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(json_text({'pid': os.getpid(), 'started_at': now()}))
        yield
    finally:
        path.unlink(missing_ok=True)


def validate(record):
    if not isinstance(record, dict):
        raise ValueError('每条证据必须是 JSON 对象')
    record = dict(record)
    for key in ('title', 'summary', 'url', 'publisher', 'region', 'category', 'status', 'checked_date'):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f'缺少必填字段: {key}')
        record[key] = record[key].strip()
    url = urlsplit(record['url'])
    if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
        raise ValueError('url 必须是无凭据的公开 http/https 来源链接')
    record['url'] = urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path, url.query, ''))
    if record['status'] not in STATUSES:
        raise ValueError('不支持的 status')
    for key in ('published_date', 'event_date', 'checked_date'):
        value = record.get(key)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError(f'{key} 必须是 YYYY-MM-DD，未知时使用 null')
            if date.fromisoformat(value) > datetime.now(TZ).date() and key != 'event_date':
                raise ValueError(f'{key} 不能在未来')
    if record.get('published_date') and record['published_date'] > record['checked_date']:
        raise ValueError('核验日期不能早于发布日期')
    if record.get('event_date') and record['event_date'] > record['checked_date'] and record['status'] not in ('planned', 'needs_verification'):
        raise ValueError('未来事件必须标记为 planned 或 needs_verification')
    record.setdefault('published_date', None)
    record.setdefault('event_date', None)
    record.setdefault('source_type', 'primary')
    record.setdefault('tags', [])
    if not isinstance(record['tags'], list) or any(not isinstance(t, str) for t in record['tags']):
        raise ValueError('tags 必须是字符串数组')
    for key in ('review_after', 'valid_until'):
        if record.get(key) is not None:
            value = record[key]
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError(f'{key} 必须是 YYYY-MM-DD 或 null')
            date.fromisoformat(value)
    if record.get('review_after') and record['review_after'] < record['checked_date']:
        raise ValueError('review_after 不能早于核验日期')
    if record.get('source_id') is not None and not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,79}', str(record['source_id'])):
        raise ValueError('source_id 必须是安全标识')
    if record.get('superseded_by') is not None and not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,79}', str(record['superseded_by'])):
        raise ValueError('superseded_by 必须是安全证据 id')
    if 'metrics' in record:
        if not isinstance(record['metrics'], list):
            raise ValueError('metrics 必须是数组')
        for metric in record['metrics']:
            if not isinstance(metric, dict) or any(not metric.get(k) for k in ('name', 'unit', 'period', 'geography', 'scope', 'basis', 'nature')):
                raise ValueError('数字必须包含名称、单位、期间、地区、统计对象、依据与实际/规划性质')
            if isinstance(metric.get('value'), bool) or not isinstance(metric.get('value'), (int, float)):
                raise ValueError('metric.value 必须是数字')
    record.setdefault('id', hashlib.sha256((record['url'] + '\n' + record['title']).encode()).hexdigest()[:16])
    if not isinstance(record['id'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,79}', record['id']):
        raise ValueError('id 必须是安全的小写字母、数字、下划线或连字符')
    # System-controlled timestamps cannot be supplied by an importer.
    for key in ('created_at', 'updated_at'):
        record.pop(key, None)
    return record


def render(record):
    lines = [f"# {record['title']}", '',
             f"- 证据编号：{record['id']}",
             f"- 状态：{LABELS[record['status']]}",
             f"- 发布机构：{record['publisher']}",
             f"- 来源：[{record['publisher']}]({record['url']})",
             f"- 发布日期：{record['published_date'] or '未明确'}",
             f"- 事件日期：{record['event_date'] or '未明确/不适用'}",
             f"- 最后核验：{record['checked_date']}",
             f"- 地区：{record['region']}；分类：{record['category']}",
             f"- 标签：{'、'.join(record['tags'])}", '', record['summary'], '']
    if record.get('limitations'):
        lines.extend(['## 适用范围与限制', '', record['limitations'], ''])
    for key, label in (('source_id', '来源编号'), ('review_after', '复核日期'),
                       ('valid_until', '已知有效截止'), ('superseded_by', '替代证据')):
        if record.get(key):
            lines.extend([f'- {label}：{record[key]}', ''])
    if record.get('metrics'):
        lines.extend(['## 数据口径', '', '```json', json_text(record['metrics']).strip(), '```', ''])
    return '\n'.join(lines)


def reindex(root):
    db = root / 'data' / 'search.sqlite3'
    db.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    paths = [] if (root / 'config/industries.json').exists() else [root / 'README.md']
    for folder in ('topics', 'evidence', 'reports'):
        paths.extend(sorted((root / folder).rglob('*.md')))
    for path in paths:
        if (root / 'config/industries.json').exists() and path.is_relative_to(root / 'reports') and not path.is_relative_to(root / 'reports/updates'):
            continue
        if not path.is_file():
            continue
        body = path.read_text(encoding='utf-8-sig')
        title = next((line.lstrip('# ').strip() for line in body.splitlines() if line.startswith('# ')), path.stem)
        rows.append((path.relative_to(root).as_posix(), title, body))
    with closing(sqlite3.connect(db)) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY, title TEXT, body TEXT)')
        conn.execute('DELETE FROM documents')
        conn.executemany('INSERT INTO documents VALUES (?, ?, ?)', rows)
        conn.commit()
    return len(rows)


def import_records(root, payload):
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not payload:
        raise ValueError('导入内容应为非空对象或数组')
    records = [validate(record) for record in payload]
    if len({r['id'] for r in records}) != len(records):
        raise ValueError('同一批导入不能重复使用证据 id')
    counts = {'inserted': 0, 'updated': 0, 'unchanged': 0}
    with lock(root):
        for record in records:
            path = root / 'evidence' / (record['id'] + '.json')
            old = read_json(path) if path.exists() else None
            previous = {k: v for k, v in (old or {}).items() if k not in ('created_at', 'updated_at')}
            if old and previous == record:
                counts['unchanged'] += 1
                # Recover a missing generated note without rewriting the evidence.
                atomic_write(path.with_suffix('.md'), render(old))
                continue
            if old:
                snapshot = root / 'history' / record['id'] / (datetime.now(TZ).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:8] + '.json')
                atomic_write(snapshot, json_text(old))
            record['created_at'] = old['created_at'] if old else now()
            record['updated_at'] = now()
            atomic_write(path, json_text(record))
            atomic_write(path.with_suffix('.md'), render(record))
            counts['updated' if old else 'inserted'] += 1
        counts['indexed_documents'] = reindex(root)
        log_path = root / 'logs' / 'imports.jsonl'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'at': now(), **counts}, ensure_ascii=False) + '\n')
    return counts


def search(root, query, limit=10):
    terms = [t.casefold() for t in query.split() if t.strip()]
    if not terms:
        raise ValueError('请输入搜索关键词')
    db = root / 'data' / 'search.sqlite3'
    if not db.exists():
        with lock(root):
            reindex(root)
    # Literal substring matching works for Chinese, even two-character terms.
    # All terms must match; no tokenizer, model, API key or network is needed.
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute('SELECT path, title, body FROM documents').fetchall()
    results = []
    for path, title, body in rows:
        text = body.casefold()
        if not all(term in text for term in terms):
            continue
        score = sum(text.count(term) + 8 * title.casefold().count(term) for term in terms)
        position = min(text.find(term) for term in terms)
        snippet = re.sub(r'\s+', ' ', body[max(0, position - 40):position + 220])
        results.append({'path': path, 'title': title, 'score': score, 'snippet': snippet})
    return sorted(results, key=lambda r: (-r['score'], r['path']))[:limit]


def status(root):
    records = [read_json(p) for p in sorted((root / 'evidence').glob('*.json'))]
    counts = {state: sum(r['status'] == state for r in records) for state in sorted(STATUSES)}
    return {'root': str(root), 'evidence_count': len(records), 'statuses': counts,
            'last_checked': max((r['checked_date'] for r in records), default=None),
            'index_exists': (root / 'data' / 'search.sqlite3').exists()}


def main():
    # Keep storage functions usable for legacy single-industry callers.
    from workspace import run_cli
    run_cli()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f'失败：{error}', file=sys.stderr)
        sys.exit(1)
