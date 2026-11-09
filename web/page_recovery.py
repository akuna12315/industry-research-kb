"""Bounded scene failures and resumable, non-published page checkpoints."""
from copy import deepcopy

from pydantic import ValidationError

from .service import kb, ws
from .knowledge import redact
from .model_diagnostics import FIELDS
from .research_pages import ResearchPages
from .visual_pages import VisualPlan, VisualScene
from .page_scope import personal_material_request

SCENE_ATTEMPTS = 3
RECOVERY_ATTEMPTS = 2


class PagePaused(ValueError):
    pass


def scene_feedback(exc):
    if isinstance(exc, ValidationError):
        result = {'code': 'schema', 'fields': [
            {'path': [str(k) if type(k) is int else k if k in FIELDS else 'field' for k in e['loc'][:8]],
             'type': e['type'] if e['type'] in ('missing', 'literal_error', 'extra_forbidden', 'string_too_long',
                 'list_too_long', 'string_pattern_mismatch') else 'validation_error'}
            for e in exc.errors(include_input=False, include_url=False)[:8]],
            'hint': '仅修正列出的字段，使用工具schema中的字段、枚举与长度限制。'}
        if any(e['loc'] == ('chart', 'references') for e in exc.errors(include_input=False)):
            result['hint'] = 'chart层没有references；每个数据点分别设置chart.items[i].references和quote。'
        return result
    typed_code = getattr(exc, 'code', None)
    if typed_code:
        paths = getattr(exc, 'issues', []) or [getattr(exc, 'field_path', [])]
        hints = {'reference': '该数据点的references无效或不属于已选资料；使用repair_sources返回的真实chart_ref。',
                 'quote': '优先改选repair_sources中的visual_supports.support_ref并省略quote，由后端填入原话；不手抄、拼接或改写引文。',
                 'data': '所列字段未得到quote支持：选择repair_sources中的support_ref及numbers原值，或补查真实口径。不能猜数或改写quote；图形条件不足时改用表格。',
                 'table_shape': 'nodes表示行、dimensions表示列，cells恰好覆盖所有行列且无重复；行列id须匹配，清空edges和chart。',
                 'source_changed': '来源版本或授权已变化，暂停并重新检索，不能重试旧引用。'}
        return {'code': typed_code, 'fields': [{'path': p, 'type': 'value_error'} for p in paths[:8] if p],
                'hint': hints.get(typed_code, '修复列出的字段，不修改已通过场景。')}
    message = str(exc)
    path = list(getattr(exc, 'field_path', ()))
    code, hint = next(((code, hint) for words, code, hint in (
        (('权限', '授权', '版本', '超出当前', '限定信源'), 'source_changed', '来源版本或授权已变化，暂停后重新检索，不重试旧引用。'),
        (('quote', '逐字'), 'quote', '从所引用的chart_ref片段逐字复制quote；引用存在不代表摘录一致。不要拼接或改写原文。'),
        (('数值', '口径', '日期', '总量', '整体', '合计'), 'data', '保留真实来源及可比口径；未知值不能填0，不能猜数。'),
        (('chart_ref', '引用'), 'reference', 'references只能使用已检索到的chart_ref，不使用资料id或自行构造编号。'),
        (('cell', '维度', 'comparison'), 'comparison', '每个选项的每个维度恰好一个cell；缺少内容明确标unknown。'),
        (('节点', '关系', '流程'), 'structure', '检查节点id、关系端点、重复关系及流程循环。'),
    ) if any(word in message for word in words)), ('structure', '检查场景与方案是否一致，并保持其他已通过场景不变。'))
    return {'code': code, 'fields': [{'path': path, 'type': 'value_error'}] if path else [], 'hint': hint}


def repair_sources(ctx, scene, feedback):
    if feedback['code'] not in ('quote', 'reference', 'data', 'comparison', 'structure', 'schema'):
        return []
    data = scene.model_dump() if isinstance(scene, VisualScene) else scene
    paths = feedback.get('fields', [])
    path = paths[0]['path'] if paths else []
    item, parents = data, [data]
    for key in path:
        try:
            item = item[int(key)] if isinstance(item, list) else item[key]
            parents.append(item)
        except (KeyError, IndexError, TypeError, ValueError):
            break
    item = next((p for p in reversed(parents) if isinstance(p, dict) and 'references' in p), data)
    def refs(value):
        if isinstance(value, dict):
            return list(value.get('references', [])) + [r for k, v in value.items() if k != 'references' for r in refs(v)]
        if isinstance(value, list):
            return [r for v in value for r in refs(v)]
        return []
    selected = [ref for ref in refs(item) if ref in ctx.chart_sources]
    if not selected:
        selected = [ref for ref in refs(data) if ref in ctx.chart_sources]
    if not selected:
        selected = list(ctx.chart_sources)
    result = []
    from .charts import _source
    for ref in dict.fromkeys(selected):
        try:
            source = _source(ctx, ref)
        except ValueError as exc:
            if getattr(exc, 'code', None) == 'reference':
                continue
            raise PagePaused('资料版本或授权已变化，请重新检索') from None
        body, quote = source.get('text', ''), item.get('quote', '')
        position = body.find(quote) if quote else -1
        start = max(0, position - 200) if position >= 0 else 0
        from .visual_supports import source_supports
        result.append({'chart_ref': ref, 'title': source.get('title'), 'location': source.get('location'),
                       'text': body[start:start+1600], 'support_quotes': source.get('support_quotes', [])[:8],
                       'visual_supports': source_supports(ctx,ref)})
        if len(result) >= 4:
            break
    return result


class PageDrafts:
    def __init__(self, store):
        self.store = store
        self.directory = store.root / 'reports/page-drafts'

    def path(self, key):
        return self.directory / (ws.safe_id(key) + '.json')

    def get(self, key):
        path = self.path(key)
        if not path.exists():
            raise ValueError('生成草稿不存在，请重新发起任务')
        draft = kb.read_json(path)
        ws.selected(self.store.root, draft['industry'], many=True)
        return draft

    @staticmethod
    def brief(draft):
        outlines = draft.get('plan', {}).get('scenes', [])
        completed = set(draft.get('scenes', {}))
        return {'id': draft['id'], 'industry': draft['industry'], 'status': draft['status'],
                'research': draft.get('options', {}).get('research', False),
                'pause_reason': draft.get('pause_reason'),
                'title': draft.get('plan', {}).get('title') or '未完成的可视化页面',
                'completed': len(completed), 'total': len(outlines),
                'remaining': [s['id'] for s in outlines if s['id'] not in completed],
                'failures': draft.get('failures', {}), 'updated_at': draft['updated_at'],
                'materials': draft.get('options', {}).get('page_materials', []),
                'request': draft.get('request', ''),
                'needs_material_selection': not draft.get('options', {}).get('page_materials') and personal_material_request(draft.get('request', ''))}

    def listing(self, industry='all'):
        allowed = {e['id'] for e in ws.selected(self.store.root, industry, many=True)}
        if industry == 'all':
            allowed.add('all')
        result = []
        for path in self.directory.glob('*.json'):
            draft = kb.read_json(path)
            if draft['industry'] not in allowed:
                continue
            if draft['status'] != 'completed' and (industry == 'all' or draft['industry'] == industry):
                result.append(self.brief(draft))
        return sorted(result, key=lambda d: d['updated_at'], reverse=True)

    def sources(self, ctx):
        sources = []
        for source in ctx.chart_sources.values():
            # Tools already return bounded material snippets. Public-page previews
            # are capped here too; whole downloaded pages and model messages stay out.
            clean = {k: deepcopy(source.get(k)) for k in (
                'ref', 'kind', 'id', 'industry_id', 'title', 'url', 'checked_date',
                'published_date', 'event_date', 'effective_date', 'status', 'region', 'citation',
                'location', 'retrieved_fingerprint', 'source_kind', 'related_material', 'metrics')}
            clean['text'] = source.get('text', '')[:1600]
            from .charts import _excerpt_supported
            quotes = [entry['quote'] for entry in ctx.visual_supports.values()
                      if entry['reference'] == source['ref'] and _excerpt_supported(entry['quote'],source)]
            if quotes:
                clean['support_quotes'] = list(dict.fromkeys(quotes))
            sources.append(clean)
        # Preserve exact supports used by valid scenes, never invalid tool inputs.
        from .research_pages import PageSpec, validate_page
        if ctx.visual_scenes:
            plan = ctx.visual_plan.model_dump(exclude={'scenes'})
            supports = {}
            selected = ctx.page_scene_id
            try:
                ctx.page_scene_id = None
                for scene in ctx.visual_scenes.values():
                    checked = validate_page(ctx, PageSpec.model_validate({**plan, 'schema_version': 2,
                        'scenes': [scene.model_dump()]}))
                    for source in checked['sources']:
                        supports.setdefault(source['ref'], []).extend(source.get('support_quotes', []))
            finally:
                ctx.page_scene_id = selected
            for source in sources:
                if supports.get(source['ref']):
                    source['support_quotes'] = list(dict.fromkeys([*source.get('support_quotes',[]),*supports[source['ref']]]))
        return sources

    def save(self, ctx, status='running'):
        if not ctx.visual_plan or not ctx.page_checkpoint:
            return None
        data = {**ctx.page_checkpoint, 'status': status, 'updated_at': kb.now(),
                'plan': ctx.visual_plan.model_dump(),
                'scenes': {key: s.model_dump() for key, s in ctx.visual_scenes.items()},
                'failures': deepcopy(ctx.scene_failures), 'scene_revisions': deepcopy(ctx.scene_revisions),
                'visual_supports': deepcopy(ctx.visual_supports), 'sources': self.sources(ctx)}
        text = kb.json_text(data)
        if len(text) > 700000 or redact(text) != text:
            raise ValueError('生成草稿过大或含疑似凭据，无法写入检查点')
        with kb.lock(self.store.root):
            kb.atomic_write(self.path(data['id']), text)
        return self.brief(data)

    def restore(self, ctx, draft):
        issues = ResearchPages(self.store).warnings({'sources': draft['sources']}, draft['industry'])
        if issues:
            raise ValueError('草稿的来源版本或授权已变化，请重新检索后生成')
        ctx.visual_plan = VisualPlan.model_validate(draft['plan'])
        ctx.scene_revisions = deepcopy(draft.get('scene_revisions', {}))
        ctx.page_resuming = True
        ctx.visual_scenes = {key: VisualScene.model_validate(scene) for key, scene in draft['scenes'].items()}
        ctx.chart_sources.update({s['ref']: deepcopy(s) for s in draft['sources']})
        ctx.visual_supports = deepcopy(draft.get('visual_supports', {}))
        from .charts import _source, _excerpt_supported
        for entry in ctx.visual_supports.values():
            if not _excerpt_supported(entry['quote'],_source(ctx,entry['reference'])):
                raise ValueError('保存的摘录与资料版本不一致，请重新检索')
        for source in draft['sources']:
            if source['kind'] == 'page':
                ctx.pages[source['url']] = deepcopy(source)
        # A user-initiated resume opens a new bounded attempt window.
        ctx.scene_failures = {}

    def complete(self, key, page):
        with kb.lock(self.store.root):
            old = self.get(key)
            kb.atomic_write(self.path(key), kb.json_text({
                'id': key, 'industry': old['industry'], 'status': 'completed',
                'page_id': page['id'], 'updated_at': kb.now()}))


def recovery_prompt(ctx):
    """Only a bounded local checkpoint is sent, never raw failed response bodies."""
    sources = PageDrafts(ctx.store).sources(ctx)
    return ('继续修复未完成的可视化页面。已通过的场景保持不变，禁止重新检索整份资料或重建整个页面。'
            '仅对remaining逐个调用add_visual_scene，完成后finish_visual_page。引用摘录必须逐字来自sources；'
            '图表数据条件不满足时可调用revise_visual_scene，将未通过的chart改为有引用的精确表格；'
            '标记quarantined的场景本轮已耗尽次数，继续其他场景后结束并保留草稿。\n' + kb.json_text({
                'plan': ctx.visual_plan.model_dump(), 'completed': list(ctx.visual_scenes),
                'remaining': [s.id for s in ctx.visual_plan.scenes if s.id not in ctx.visual_scenes],
                'failures': ctx.scene_failures, 'sources': sources, 'visual_supports': ctx.visual_supports}))
