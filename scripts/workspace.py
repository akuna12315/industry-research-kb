"""Industry profiles, source management, freshness and auditable briefs.

This module never downloads sources or creates factual claims.
"""
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
import re
from urllib.parse import urlsplit

import kb
import professions

DEFAULT_CATEGORIES = ['政策与监管', '企业与竞争', '产品与技术', '供应链', '市场与财务', '项目与事件', '风险', '背景与术语']
FRESHNESS_LABELS = {'current': '未到复核期限', 'review_due': '待复核',
                    'expired': '已过已知有效期', 'superseded': '已被替代', 'needs_verification': '待核验'}


def safe_id(value):
    if not isinstance(value, str) or value == 'all' or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,79}', value):
        raise ValueError('标识需为3至80位小写字母、数字、下划线或连字符，不能使用 all')
    return value


def registry(root):
    path = root / 'config/industries.json'
    if not path.exists():
        return {'version': 1, 'active_industry': 'legacy', 'industries': [
            {'id': 'legacy', 'name': '原有知识库', 'path': '.', 'enabled': True}]}
    result = kb.read_json(path)
    entries = result.get('industries', [])
    ids = [safe_id(p['id']) for p in entries]
    if len(set(ids)) != len(ids) or (result.get('active_industry') is not None and result['active_industry'] not in ids):
        raise ValueError('行业注册表含重复或未知标识')
    for item in entries:
        if not isinstance(item.get('enabled'), bool):
            raise ValueError('行业 enabled 必须是布尔值')
        profile_path(root, item)
    return result


def profile_path(root, entry):
    path = (root / entry['path']).resolve()
    if path != root.resolve() and not path.is_relative_to((root / 'industries').resolve()):
        raise ValueError('行业目录必须为库根目录或 industries/ 内的目录')
    return path


def selected(root, industry=None, many=False):
    config = registry(root)
    industry = industry or config.get('active_industry')
    if industry == 'all' or (not industry and many):
        return config['industries']
    if not industry:
        raise ValueError('尚未选择行业；请指定 --industry 或运行 industry use')
    for item in config['industries']:
        if item['id'] == industry:
            return [item]
    raise ValueError(f'未知行业: {industry}')


def scope(root):
    path = root / 'config/scope.json'
    return kb.read_json(path) if path.exists() else {}


def freshness(record, settings, as_of=None):
    today = as_of or datetime.now(kb.TZ).date()
    policy = settings.get('freshness', {})
    ttl = policy.get('category_days', {}).get(record['category'], policy.get('default_days', 30))
    due = date.fromisoformat(record['review_after']) if record.get('review_after') else date.fromisoformat(record['checked_date']) + timedelta(days=ttl)
    if record['status'] == 'superseded':
        state = 'superseded'
    elif record.get('valid_until') and today > date.fromisoformat(record['valid_until']):
        state = 'expired'
    elif record['status'] == 'needs_verification':
        state = 'needs_verification'
    elif today >= due:
        state = 'review_due'
    else:
        state = 'current'
    return {'freshness': state, 'freshness_label': FRESHNESS_LABELS[state],
            'review_due_date': due.isoformat(), 'review_after': record.get('review_after'), 'as_of': today.isoformat(),
            'checked_date': record['checked_date'], 'published_date': record.get('published_date'),
            'event_date': record.get('event_date'), 'valid_until': record.get('valid_until')}


def records(root, entry, as_of=None):
    home = profile_path(root, entry)
    settings = scope(home)
    result = []
    for path in sorted((home / 'evidence').glob('*.json')):
        record = kb.read_json(path)
        item = {**record, 'industry_id': entry['id'], **freshness(record, settings, as_of),
                'path': path.with_suffix('.md').relative_to(root).as_posix()}
        if record.get('source_kind') == 'local_material' or record.get('related_material'):
            source = (record.get('material_source') if record.get('source_kind') == 'local_material' else record.get('related_material')) or {}
            if not isinstance(source, dict):
                source = {}
            key = source.get('material_id', '')
            material_path = home / 'materials' / (key + '.json') if re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,79}', key) else None
            changed = (material_path is None or not material_path.is_file()
                       or not kb.material_source_current(kb.read_json(material_path), source))
            item['source_version_changed'] = changed
            if changed and item['freshness'] not in ('superseded', 'expired'):
                item.update(freshness='review_due', freshness_label='原资料版本已变更，需重新核验')
        result.append(item)
    return result


def matches(record, args):
    if getattr(args, 'review_only', False) and record.get('freshness') == 'current':
        return False
    return all(not getattr(args, key, None) or record.get(key) == getattr(args, key)
               for key in ('category', 'publisher', 'source_id', 'status', 'freshness')) and (
                   not getattr(args, 'tag', None) or args.tag in record.get('tags', []))


def find(root, entries, args, *, evidence_only=False):
    results = []
    for entry in entries:
        home = profile_path(root, entry)
        evidence = {r['path']: r for r in records(root, entry)}
        if not (home / 'data/search.sqlite3').exists():
            with kb.lock(root):
                kb.reindex(home)
        # Retrieve all candidates before filtering; a high-ranked old record
        # must not hide lower-ranked fresh evidence.
        for hit in kb.search(home, args.query, 10**9):
            hit['path'] = (home / hit['path']).relative_to(root).as_posix()
            record = evidence.get(hit['path'])
            if evidence_only and record is None:
                continue
            if record:
                if not matches(record, args):
                    continue
                hit.update({k: record.get(k) for k in ('id', 'url', 'publisher', 'source_id', 'status',
                           'category', 'tags', 'checked_date', 'published_date', 'event_date',
                           'freshness', 'freshness_label', 'review_after', 'review_due_date', 'valid_until')})
            elif any(getattr(args, k, None) for k in ('category', 'publisher', 'source_id', 'status', 'freshness', 'tag')):
                continue
            else:
                hit['freshness'] = 'document_unassessed'
                hit['freshness_label'] = '专题/报告需核对所引证据日期'
            hit['industry_id'] = entry['id']
            results.append(hit)
    return sorted(results, key=lambda r: (-r['score'], r['path']))[:args.limit]


def validate_scope(config):
    for field in ('industry', 'primary_region'):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise ValueError(f'范围缺少 {field}')
    if not isinstance(config.get('topics'), list) or not config['topics'] or any(not isinstance(x, str) or not x.strip() for x in config['topics']):
        raise ValueError('topics 必须是非空分类名称数组')
    for key in ('lookback_days', 'max_new_evidence_per_run'):
        if type(config.get(key)) is not int or config[key] < 1:
            raise ValueError(f'{key} 必须是正整数')
    policy = config.get('freshness', {})
    if not isinstance(policy, dict) or not isinstance(policy.get('category_days', {}), dict):
        raise ValueError('freshness 必须包含分类天数配置')
    for value in [policy.get('default_days', 30), *policy.get('category_days', {}).values()]:
        if type(value) is not int or not 1 <= value <= 36500:
            raise ValueError('复核周期必须为1至36500天')
    if 'research_regions' in config:
        config['research_regions'] = professions.regions(config)
    if not isinstance(config.get('comparison_regions', []), list) or any(not isinstance(r, str) or not r.strip() for r in config.get('comparison_regions', [])):
        raise ValueError('对照地区应为地区名称数组')
    return config


def industry_command(root, args):
    config = registry(root)
    if args.action == 'list':
        return config
    with kb.lock(root):
        config = registry(root)
        if args.action == 'add':
            key = safe_id(args.id)
            if any(p['id'] == key for p in config['industries']):
                raise ValueError('行业已存在；不会覆盖原档案')
            home = root / 'industries' / key
            if home.exists():
                raise ValueError('目标目录已存在；请检查后选择其他标识')
            settings = validate_scope({'name': args.name + '行业知识库', 'industry': args.name,
                'primary_region': args.region, 'comparison_regions': [], 'timezone': 'Asia/Shanghai',
                'topics': args.categories or DEFAULT_CATEGORIES, 'lookback_days': 7,
                'max_new_evidence_per_run': 12, 'weekly_deep_dive_day': 'Monday',
                'freshness': {'default_days': 30, 'category_days': {}}})
            forestry = getattr(args, 'specialty', None) == 'forestry'
            if forestry:
                settings.update(specialty='forestry', query_template='forestry-v1', research_regions=[args.region],
                                topics=args.categories or professions.FORESTRY_TOPICS,
                                freshness={'default_days': 30, 'category_days': {'技术标准': 90, '政策法规与规划': 60, '资源调查与空间资料': 365}})
            for folder in ('evidence', 'history', 'topics', 'reports/updates', 'reports/briefs', 'work', 'data'):
                (home / folder).mkdir(parents=True, exist_ok=True)
            kb.atomic_write(home / 'config/scope.json', kb.json_text(settings))
            kb.atomic_write(home / 'config/sources.json', kb.json_text(professions.national_sources() if forestry else {'sources': [], 'discovery_domains': [], 'mode': 'preferred'}))
            kb.atomic_write(home / 'config/queries.json', kb.json_text(professions.forestry_queries(settings) if forestry else {category: [f'{args.name} {category} 官方'] for category in settings['topics']}))
            kb.atomic_write(home / 'README.md', f'# {args.name}行业档案\n\n地区：{args.region}。尚无证据；信息来源待指定或公开发现。\n')
            for index, category in enumerate(settings['topics'], 1):
                kb.atomic_write(home / f'topics/{index:02d}.md', f'# {category}\n\n尚无经核验资料。调查问题：关键事实、统计口径和原始来源是什么？\n')
            config['industries'].append({'id': key, 'name': args.name, 'path': f'industries/{key}', 'enabled': False})
        elif args.action == 'use':
            if args.id != 'none':
                selected(root, args.id)
            config['active_industry'] = None if args.id == 'none' else args.id
        elif args.action in ('enable', 'disable'):
            entry = next((p for p in config['industries'] if p['id'] == args.id), None)
            if not entry:
                raise ValueError('未知行业')
            entry['enabled'] = args.action == 'enable'
        kb.atomic_write(root / 'config/industries.json', kb.json_text(config))
    return config


def source_command(root, entry, args):
    home = profile_path(root, entry)
    path = home / 'config/sources.json'
    catalog = kb.read_json(path) if path.exists() else {'sources': []}
    if args.action == 'list':
        return catalog
    with kb.lock(root):
        catalog = kb.read_json(path) if path.exists() else {'sources': []}
        if args.action == 'add':
            key = safe_id(args.id)
            if any(s.get('id') == key for s in catalog['sources']):
                raise ValueError('来源编号已存在')
            parsed = urlsplit(args.url)
            if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('来源需为无凭据的公开 http/https 链接')
            catalog['sources'].append({'id': key, 'name': args.name, 'url': args.url,
                'type': args.type, 'categories': args.categories or [], 'priority': args.priority,
                'enabled': True, 'verified': False, 'checked_date': None,
                'notes': '用户指定入口，尚未打开核验；名称和地址不构成可信度证明。'})
        elif args.action == 'mode':
            catalog['mode'] = args.mode
        else:
            item = next((s for s in catalog['sources'] if s.get('id') == args.id), None)
            if not item:
                raise ValueError('未知来源编号')
            item['enabled'] = args.action == 'enable'
        kb.atomic_write(path, kb.json_text(catalog))
    return catalog


def brief(root, entries, args):
    as_of = datetime.now(kb.TZ).date()
    items = [r for p in entries for r in records(root, p) if matches(r, args)]
    if args.query:
        terms = args.query.casefold().split()
        items = [r for r in items if all(t in kb.render(r).casefold() for t in terms)]
    items.sort(key=lambda r: (r['industry_id'], r['category'], r['id']))
    lines = ['# 行业证据简报', '', f'生成日期（北京时间）：{as_of}；本地证据汇编，未执行联网复核。', '',
             '覆盖行业：' + '、'.join(p['name'] for p in entries),
             f'匹配证据：{len(items)}条；待复核/待核验/过有效期/被替代：{sum(r["freshness"] != "current" for r in items)}条。', '',
             '核验日期只表示该条来源的核验时间，不代表行业动态覆盖到当天；未到复核期限也不保证事实持续成立。', '']
    group = None
    for record in items:
        key = (record['industry_id'], record['category'])
        if key != group:
            lines.extend([f'## {key[0]} / {key[1]}', ''])
            group = key
        lines.extend([f'### {record["title"]}', '', record['summary'], '',
            f'- 资料适用地区：{record.get("region") or "未填写，须核对"}',
            f'- 事实状态：{kb.LABELS[record["status"]]}；时效：{record["freshness_label"]}',
            f'- 发布：{record.get("published_date") or "未明确"}；事件：{record.get("event_date") or "未明确"}；核验：{record["checked_date"]}；复核期限：{record["review_due_date"]}',
            (f'- [本地证据](../../{record["path"]}) · 本地资料 {record["material_source"]["material_id"]} / {record["material_source"]["citation"]}'
             if record.get('source_kind') == 'local_material' else f'- [本地证据](../../{record["path"]}) · [原始来源]({record["url"]})'),
            f'- 适用范围与限制：{record.get("limitations") or "未填写，需补充"}', ''])
        if record.get('metrics'):
            lines.extend(['数字口径：', '', '```json', kb.json_text(record['metrics']).strip(), '```', ''])
    if not items:
        lines.extend(['没有匹配的本地证据。资料缺失不表示行业中不存在相关事实。', ''])
    target = root / 'reports/briefs' / (as_of.isoformat() + '-' + kb.uuid4().hex[:8] + '.md')
    with kb.lock(root):
        kb.atomic_write(target, '\n'.join(lines))
    return {'path': str(target), 'evidence_count': len(items), 'as_of': as_of.isoformat()}


def update_plan(root):
    today = datetime.now(kb.TZ).date()
    result = []
    for entry in registry(root)['industries']:
        if not entry['enabled']:
            continue
        home = profile_path(root, entry)
        settings = validate_scope(scope(home))
        state_path = home / 'data/update-state.json'
        state = kb.read_json(state_path) if state_path.exists() else {}
        covered = state.get('covered_through')
        if covered and date.fromisoformat(covered) > today:
            raise ValueError('覆盖日期不能在未来')
        start = today - timedelta(days=settings['lookback_days'] - 1)
        if covered:
            start = min(start, date.fromisoformat(covered) + timedelta(days=1))
        catalog = kb.read_json(home / 'config/sources.json')
        questions = kb.read_json(home / 'config/queries.json')
        due = [r['id'] for r in records(root, entry) if r['freshness'] in ('review_due', 'needs_verification', 'expired')]
        categories = settings['topics']
        if today.weekday() == 0:
            counts = {c: sum(r['category'] == c and r['freshness'] == 'current' for r in records(root, entry)) for c in categories}
            focus = min(categories, key=lambda c: counts[c])
        else:
            focus = categories[today.toordinal() % len(categories)]
        result.append({'industry_id': entry['id'], 'root': str(home),
            'from': start.isoformat(), 'through': today.isoformat(),
            'already_successful_today': state.get('last_run_status') == 'success' and state.get('last_run_date') == today.isoformat(),
            'weekly_deep_dive': today.weekday() == 0, 'focus_category': focus,
            'queries': questions, 'source_mode': catalog.get('mode', 'preferred'),
            'sources': sorted([s for s in catalog['sources'] if s.get('enabled', True)], key=lambda s: s.get('priority', 3)),
            'discovery_domains': catalog.get('discovery_domains', []),
            'review_due_ids': due, 'max_new_evidence': settings['max_new_evidence_per_run'],
            'state': state})
    return {'as_of': today.isoformat(), 'write_locked': (root / 'data/write.lock').exists(), 'industries': result,
            'note': '只生成调查计划，不联网、不声称覆盖或执行成功。没有启用行业时定时任务静默跳过。'}


def filters(parser):
    parser.add_argument('--category')
    parser.add_argument('--tag')
    parser.add_argument('--publisher')
    parser.add_argument('--source-id')
    parser.add_argument('--status', choices=sorted(kb.STATUSES))
    parser.add_argument('--freshness', choices=sorted(FRESHNESS_LABELS))


def finish_run(root, entry, payload):
    """Persist human/agent-verified coverage; never infer it from import times."""
    today = datetime.now(kb.TZ).date()
    if not isinstance(payload, dict) or payload.get('status') not in ('success', 'partial', 'failed'):
        raise ValueError('更新结果需要 success/partial/failed 状态')
    for key in ('run_date', 'coverage_from', 'coverage_through'):
        value = payload.get(key)
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError(f'{key} 需要 YYYY-MM-DD')
        if date.fromisoformat(value) > today:
            raise ValueError('不能记录未来更新或覆盖日期')
    if payload['coverage_from'] > payload['coverage_through'] or payload['coverage_through'] > payload['run_date']:
        raise ValueError('覆盖区间顺序错误')
    for key in ('queries', 'sources_accessed', 'sources_failed', 'inserted_ids', 'updated_ids', 'unchanged_ids', 'open_questions', 'uncovered_ranges'):
        if not isinstance(payload.get(key), list):
            raise ValueError(f'更新结果缺少数组: {key}')
    if not isinstance(payload.get('next_focus'), str):
        raise ValueError('需要填写 next_focus')
    if payload['status'] == 'success' and (payload['sources_failed'] or payload['uncovered_ranges'] or not payload['sources_accessed']):
        raise ValueError('有失败来源、未覆盖区间或无已访问来源时不能标记 success')
    if payload['status'] == 'partial' and not payload['sources_accessed']:
        raise ValueError('全失败应标记 failed')
    if payload['status'] == 'failed' and (payload['inserted_ids'] or payload['updated_ids']):
        raise ValueError('全失败不能声明新增或修订证据')
    home = profile_path(root, entry)
    for key in ('inserted_ids', 'updated_ids', 'unchanged_ids'):
        for evidence_id in payload[key]:
            safe_id(evidence_id)
            if not (home / f'evidence/{evidence_id}.json').is_file():
                raise ValueError(f'记录引用不存在的证据: {evidence_id}')
    with kb.lock(root):
        state_path = home / 'data/update-state.json'
        state = kb.read_json(state_path) if state_path.exists() else {}
        previous = state.get('covered_through')
        if payload['status'] == 'success':
            if previous and date.fromisoformat(payload['coverage_from']) > date.fromisoformat(previous) + timedelta(days=1):
                raise ValueError('覆盖日期与上次成功区间之间有空档，应补查或记录 partial')
            state['covered_through'] = max(previous or payload['coverage_through'], payload['coverage_through'])
            state['last_success_date'] = payload['run_date']
            state['consecutive_failures'] = 0
        elif payload['status'] == 'failed':
            state['consecutive_failures'] = state.get('consecutive_failures', 0) + 1
        else:
            state['consecutive_failures'] = 0
        state.update({'last_run_date': payload['run_date'], 'last_run_status': payload['status'], 'updated_at': kb.now()})
        target = home / f'reports/updates/{payload["run_date"]}.md'
        previous_note = target.read_text(encoding='utf-8-sig') if target.exists() else f'# {payload["run_date"]} 更新记录\n'
        note = '\n\n## 本次运行 ' + kb.now() + '\n\n```json\n' + kb.json_text(payload) + '```\n'
        kb.atomic_write(target, previous_note + note)
        kb.atomic_write(state_path, kb.json_text(state))
        kb.reindex(home)
    return {'state': state, 'report': str(target)}


def run_cli():
    parser = argparse.ArgumentParser(description='通用行业知识库：行业、来源、证据检索、时效和简报')
    parser.add_argument('--root', type=Path, default=kb.DEFAULT_ROOT)
    parser.add_argument('--industry', help='行业编号；检索、汇报和状态可用 all')
    commands = parser.add_subparsers(dest='command', required=True)
    find_parser = commands.add_parser('search')
    find_parser.add_argument('query')
    find_parser.add_argument('--limit', type=int, default=10)
    filters(find_parser)
    add = commands.add_parser('import')
    add.add_argument('file', type=Path)
    commands.add_parser('reindex')
    commands.add_parser('status')
    stale = commands.add_parser('stale')
    filters(stale)
    report = commands.add_parser('report')
    report.add_argument('--query')
    filters(report)
    commands.add_parser('update-plan')
    configure = commands.add_parser('configure', help='用JSON局部更新行业范围、分类和复核周期')
    configure.add_argument('file', type=Path)
    finish = commands.add_parser('finish-run', help='保存真实的联网执行结果和覆盖日期')
    finish.add_argument('file', type=Path)
    industry = commands.add_parser('industry').add_subparsers(dest='action', required=True)
    industry.add_parser('list')
    create = industry.add_parser('add')
    create.add_argument('id')
    create.add_argument('name')
    create.add_argument('--region', default='中国')
    create.add_argument('--categories', nargs='+')
    create.add_argument('--specialty', choices=['forestry'])
    for action in ('use', 'enable', 'disable'):
        industry.add_parser(action).add_argument('id')
    sources = commands.add_parser('source').add_subparsers(dest='action', required=True)
    sources.add_parser('list')
    create_source = sources.add_parser('add')
    create_source.add_argument('id')
    create_source.add_argument('name')
    create_source.add_argument('url')
    create_source.add_argument('--type', default='primary')
    create_source.add_argument('--categories', nargs='+')
    create_source.add_argument('--priority', type=int, choices=range(1, 6), default=1)
    sources.add_parser('mode').add_argument('mode', choices=('preferred', 'only'))
    for action in ('enable', 'disable'):
        sources.add_parser(action).add_argument('id')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == 'industry':
        result = industry_command(root, args)
    elif args.command == 'update-plan':
        result = update_plan(root)
    else:
        entries = selected(root, args.industry, many=args.command in ('search', 'stale', 'report', 'status', 'reindex'))
        if args.command in ('import', 'source', 'configure', 'finish-run') and len(entries) != 1:
            raise ValueError('写入需要指定单个行业')
        if args.command == 'search':
            if not 1 <= args.limit <= 100:
                raise ValueError('--limit 应在1至100之间')
            result = find(root, entries, args)
        elif args.command == 'source':
            result = source_command(root, entries[0], args)
        elif args.command == 'finish-run':
            result = finish_run(root, entries[0], kb.read_json(args.file))
        elif args.command == 'configure':
            home = profile_path(root, entries[0])
            changes = kb.read_json(args.file)
            if not isinstance(changes, dict):
                raise ValueError('范围配置必须是JSON对象')
            with kb.lock(root):
                old = scope(home)
                updated = {**old, **changes}
                if 'primary_region' in changes and 'research_regions' not in changes:
                    updated['research_regions'] = [changes['primary_region']]
                updated = validate_scope(updated)
                professions.sync_queries(home, old, updated, kb)
                kb.atomic_write(home / 'config/scope.json', kb.json_text(updated))
            result = updated
        elif args.command == 'import':
            home = profile_path(root, entries[0])
            payload = kb.read_json(args.file)
            # One workspace lock serializes changes across every profile.
            if home == root:
                result = kb.import_records(home, payload)
            else:
                with kb.lock(root):
                    result = kb.import_records(home, payload)
        elif args.command == 'reindex':
            with kb.lock(root):
                result = {entry['id']: kb.reindex(profile_path(root, entry)) for entry in entries}
        elif args.command == 'report':
            result = brief(root, entries, args)
        elif args.command == 'stale':
            result = [r for entry in entries for r in records(root, entry) if r['freshness'] != 'current' and matches(r, args)]
        else:
            result = {'active_industry': registry(root).get('active_industry'), 'industries': []}
            for entry in entries:
                home = profile_path(root, entry)
                evidence = records(root, entry)
                result['industries'].append({**entry, **kb.status(home),
                    'freshness_counts': {key: sum(r['freshness'] == key for r in evidence) for key in FRESHNESS_LABELS}})
    print(kb.json_text(result))
