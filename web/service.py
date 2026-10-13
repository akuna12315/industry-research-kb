"""Small adapter around the existing evidence store, shared by UI and agent."""
import argparse
from datetime import datetime
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import kb
import workspace as ws


class Store:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()

    def entry(self, industry):
        if industry == 'all':
            raise ValueError('此操作需要单个行业')
        return ws.selected(self.root, industry)[0]

    def home(self, industry):
        return ws.profile_path(self.root, self.entry(industry))

    def args(self, **values):
        result = dict(query=None, limit=50, category=None, tag=None, publisher=None,
                      source_id=None, status=None, freshness=None, review_only=False)
        result.update(values)
        return argparse.Namespace(**result)

    def state(self):
        registry = ws.registry(self.root)
        entries = []
        for entry in registry['industries']:
            home = ws.profile_path(self.root, entry)
            evidence = ws.records(self.root, entry)
            state = home / 'data/update-state.json'
            entries.append({**entry, 'scope': ws.scope(home), 'evidence_count': len(evidence),
                'review_count': sum(r['freshness'] != 'current' for r in evidence),
                'last_checked': max((r['checked_date'] for r in evidence), default=None),
                'update_state': kb.read_json(state) if state.exists() else {}})
        return {'active_industry': registry.get('active_industry'), 'industries': entries,
                'as_of': datetime.now(kb.TZ).date().isoformat(),
                'automation': kb.read_json(self.root / 'config/automation.json'),
                'write_locked': (self.root / 'data/write.lock').exists()}

    def evidence(self, industry='all', query='', **filters):
        entries = ws.selected(self.root, industry, many=True)
        args = self.args(**filters)
        if query.strip():
            args.query = query.strip()
            hits = ws.find(self.root, entries, args, evidence_only=True)
            by_id = {(r['industry_id'], r['id']): r for e in entries for r in ws.records(self.root, e)}
            return [by_id[(h['industry_id'], h['id'])] for h in hits if h.get('id')]
        return sorted([r for e in entries for r in ws.records(self.root, e) if ws.matches(r, args)],
                      key=lambda r: (r['checked_date'], r['id']), reverse=True)[:args.limit]

    def sources(self, industry):
        return ws.source_command(self.root, self.entry(industry), argparse.Namespace(action='list'))

    def save_sources(self, industry, catalog):
        if not isinstance(catalog, dict) or catalog.get('mode') not in ('only', 'preferred') or not isinstance(catalog.get('sources'), list):
            raise ValueError('来源配置格式错误')
        ids = set()
        for item in catalog['sources']:
            key = ws.safe_id(item.get('id'))
            if key in ids:
                raise ValueError('来源编号重复')
            ids.add(key)
            url = urlsplit(item.get('url', ''))
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
                raise ValueError('来源地址须为公开无凭据HTTP链接')
            if not isinstance(item.get('name'), str) or not item['name'].strip():
                raise ValueError('来源名称不能为空')
            if type(item.get('enabled')) is not bool or type(item.get('priority')) is not int or not 1 <= item['priority'] <= 5:
                raise ValueError('来源开关或优先级错误')
            if not isinstance(item.get('categories', []), list) or any(not isinstance(c, str) for c in item.get('categories', [])):
                raise ValueError('来源分类必须是字符串数组')
        home = self.home(industry)
        old = self.sources(industry)
        old_by_id = {s.get('id'): s for s in old['sources']}
        for item in catalog['sources']:
            prior = old_by_id.get(item['id'], {})
            # UI edits cannot claim a successful verification.
            if prior.get('url') == item['url']:
                for key in ('verified', 'checked_date', 'last_access_status'):
                    item[key] = prior.get(key)
            else:
                item.update(verified=False, checked_date=None, last_access_status=None)
        with kb.lock(self.root):
            kb.atomic_write(home / 'config/sources.json', kb.json_text({**old, **catalog}))
        return catalog

    def save_scope(self, industry, changes):
        home = self.home(industry)
        with kb.lock(self.root):
            updated = ws.validate_scope({**ws.scope(home), **changes})
            kb.atomic_write(home / 'config/scope.json', kb.json_text(updated))
            for index, category in enumerate(updated['topics'], 1):
                filename = f'{index:02d}-' + kb.hashlib.sha256(category.encode()).hexdigest()[:8] + '.md'
                existing = [p for p in (home / 'topics').glob('*.md') if p.read_text(encoding='utf-8-sig').splitlines()[:1] == ['# ' + category]]
                if not existing:
                    kb.atomic_write(home / 'topics' / filename, f'# {category}\n\n调查问题待补充，尚无经核验事实。\n')
            kb.reindex(home)
        return updated

    def import_evidence(self, industry, payload):
        home = self.home(industry)
        if home == self.root:
            return kb.import_records(home, payload)
        with kb.lock(self.root):
            return kb.import_records(home, payload)

    def documents(self):
        paths = list((self.root / 'reports/briefs').glob('*.md'))
        paths += list((self.root / 'reports/maintenance').glob('*.md'))
        for entry in ws.registry(self.root)['industries']:
            paths += list((ws.profile_path(self.root, entry) / 'reports/updates').glob('*.md'))
        result = []
        for path in sorted(set(paths), key=lambda p: p.stat().st_mtime, reverse=True):
            text = path.read_text(encoding='utf-8-sig')
            result.append({'path': path.relative_to(self.root).as_posix(),
                           'title': text.splitlines()[0].lstrip('# ') if text else path.stem})
        return result[:100]

    def read_document(self, path):
        target = (self.root / path).resolve()
        allowed = [self.root / 'reports', self.root / 'topics', self.root / 'evidence']
        for entry in ws.registry(self.root)['industries']:
            home = ws.profile_path(self.root, entry)
            allowed += [home / name for name in ('reports', 'topics', 'evidence')]
        if target.suffix != '.md' or not any(target.is_relative_to(p.resolve()) for p in allowed):
            raise ValueError('只能读取知识库中的专题、证据和报告Markdown')
        return target.read_text(encoding='utf-8-sig')
