"""Local, explicitly requested research artifacts with immutable revisions."""
from copy import deepcopy
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .service import kb, ws
from .charts import ResearchChart, _source, _evidence_fingerprint, validate_chart
from .knowledge import Knowledge, redact
from .visual_pages import VisualScene, unique_ids, validate_scene

PAGE_OUTPUT_TOKENS = 12288


class PageItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str = Field(default='', max_length=160)
    text: str = Field(default='', max_length=1800)
    cells: list[str] = Field(default_factory=list, max_length=8)
    references: list[str] = Field(default_factory=list, max_length=4)
    nature: Literal['reported', 'planned', 'inference', 'unknown'] = 'reported'


class PageBlock(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['prose', 'cards', 'table', 'chart']
    title: str = Field(min_length=1, max_length=160)
    columns: list[str] = Field(default_factory=list, max_length=8)
    items: list[PageItem] = Field(default_factory=list, max_length=30)
    chart: ResearchChart | None = None


class PageSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1, 2] = 1
    title: str = Field(min_length=1, max_length=160)
    subtitle: str = Field(default='', max_length=400)
    summary: str = Field(min_length=1, max_length=1500)
    summary_references: list[str] = Field(default_factory=list, max_length=8)
    regions: list[str] = Field(default_factory=list, max_length=10)
    tags: list[str] = Field(default_factory=list, max_length=10)
    audience: str = Field(default='研究阅读', max_length=160)
    coverage: str = Field(min_length=1, max_length=1500)
    gaps: list[str] = Field(default_factory=list, max_length=15)
    blocks: list[PageBlock] = Field(default_factory=list, max_length=16)
    question: str = Field(default='', max_length=200)
    theme: Literal['sage', 'ink', 'sand'] = 'sage'
    layout: Literal['story', 'overview'] = 'story'
    scenes: list[VisualScene] = Field(default_factory=list, max_length=8)


def stored_spec(content):
    clean = deepcopy(content)
    for field in ('blocks', 'scenes'):
        for item in clean.get(field, []):
            item.pop('figure', None)
    return PageSpec.model_validate(clean)


def visual_draft(ctx):
    if ctx.visual_plan is None:
        raise ValueError('请先调用plan_visual_page设计页面')
    plan = ctx.visual_plan
    if set(ctx.visual_scenes) != {s.id for s in plan.scenes}:
        raise ValueError('尚有场景未完成，不能保存不完整页面')
    content = {**plan.model_dump(exclude={'scenes'}), 'schema_version': 2,
               'scenes': [ctx.visual_scenes[s.id].model_dump() for s in plan.scenes]}
    if ctx.page_scene_id:
        content = deepcopy(ctx.page_edit_content)
        content['scenes'] = [ctx.visual_scenes[s['id']].model_dump() if s['id'] == ctx.page_scene_id
                             else s for s in content['scenes']]
    return validate_page(ctx, stored_spec(content))


def page_requested(prompt):
    """Only affirmative artifact requests; an ordinary question stays read-only."""
    import re
    if re.search(r'(不要|不需要|不用|别)[^。\n]{0,30}(生成|制作|做成|页面)|(?:如何|怎么|能否|是否)[^。\n]{0,20}(生成|制作|创建)|功能|大纲|开发计划', prompt):
        return False
    return bool(re.search(r'(生成|制作|做成|整理成|创建|做[一个张份]*)[^。\n]{0,45}(展示页|研究页面|前端页面|可视化页面|讲解页|交互页面|专题页|网页|页面)', prompt))


def validate_page(ctx, spec):
    """Bind every supplied reference to a real retrieved source, never model URLs."""
    sources = {}

    def bind(refs):
        for ref in refs:
            source = _source(ctx, ref)
            if source['kind'] == 'evidence':
                current = next((r for r in ctx.store.evidence(source['industry_id'], limit=10**9) if r['id'] == source['id']), None)
                if not current or _evidence_fingerprint(current) != source.get('retrieved_fingerprint'):
                    raise ValueError('引用证据版本已变化，请重新检索后生成页面')
            clean = {key: source.get(key) for key in (
                'ref', 'kind', 'id', 'industry_id', 'title', 'url', 'checked_date',
                'published_date', 'event_date', 'effective_date', 'status', 'region',
                'citation', 'location', 'retrieved_fingerprint', 'source_kind', 'related_material')}
            clean['text'] = source.get('text', '')[:1600]
            clean['metrics'] = (source.get('metrics') or [])[:20]
            if sources.get(ref, {}).get('support_quotes'):
                clean['support_quotes'] = list(sources[ref]['support_quotes'])
            parsed = urlsplit(clean.get('url') or '')
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                clean['url'] = ''
            sources[ref] = clean

    content = spec.model_dump()
    if spec.schema_version == 1 and (not spec.blocks or spec.scenes):
        raise ValueError('旧格式页面需要blocks；交互图形请使用schema_version=2')
    if spec.schema_version == 2 and (not spec.scenes or spec.blocks or not spec.question.strip()):
        raise ValueError('可视化页面需要question与scenes，不接受文字blocks')
    unique_ids(spec.scenes, '场景')
    if sum(s.emphasis == 'primary' for s in spec.scenes) > 1:
        raise ValueError('一个页面最多一个primary主视觉')
    bind(spec.summary_references)
    initial_count = ctx.chart_count
    ctx.chart_count = 0  # A saved page has its own three-figure budget.
    try:
        for scene, clean in zip(spec.scenes, content['scenes']):
            validate_scene(ctx, scene, bind)
            for item in [*scene.nodes, *scene.edges, *scene.cells]:
                if not item.quote:
                    continue
                for ref in item.references:
                    # Keep the exact bounded supports used by this graph, even when
                    # they occur beyond the general 1600-character source preview.
                    from .charts import _squash
                    original = _source(ctx, ref)
                    candidates = [original.get('text', '')]
                    if getattr(ctx, 'page_operation', '') == 'layout' or getattr(ctx, 'page_resuming', False):
                        candidates += original.get('support_quotes', [])
                    if any(_squash(item.quote) in _squash(text) for text in candidates):
                        quotes = sources[ref].setdefault('support_quotes', [])
                        if item.quote not in quotes:
                            quotes.append(item.quote)
                        if len(quotes) > 24:
                            raise ValueError('同一引用最多保存24个有界图形摘录，请精简场景')
            if scene.kind == 'chart':
                clean['figure'] = validate_chart(ctx, scene.chart)
                for point in scene.chart.items:
                    bind(point.references)
                    for ref in point.references:
                        if point.quote:
                            from .charts import _excerpt_supported
                            if _excerpt_supported(point.quote, _source(ctx, ref)):
                                quotes = sources[ref].setdefault('support_quotes', [])
                                if point.quote not in quotes:
                                    quotes.append(point.quote)
        for block, clean in zip(spec.blocks, content['blocks']):
            if block.kind == 'chart':
                if block.chart is None or block.items or block.columns:
                    raise ValueError('图表模块需要chart，不接受表格行')
                clean['figure'] = validate_chart(ctx, block.chart)
                for point in block.chart.items:
                    bind(point.references)
            else:
                if block.chart is not None or not block.items:
                    raise ValueError('文字/卡片/表格模块需要items，不能携带图表')
                if block.kind == 'table' and not block.columns:
                    raise ValueError('对比表需要列标题')
                if block.kind != 'table' and block.columns:
                    raise ValueError('只有表格模块可设置列标题')
                for item in block.items:
                    if block.kind == 'table':
                        if len(item.cells) != len(block.columns) or any(len(v) > 1200 for v in item.cells):
                            raise ValueError('表格单元格数量须与列标题一致，且每格不超过1200字符')
                    elif not item.text.strip() or item.cells:
                        raise ValueError('文字与卡片需要text，不接受表格单元格')
                    if item.nature in ('reported', 'planned') and not item.references:
                        raise ValueError('来源记载及计划内容必须关联本轮引用；缺依据标为unknown')
                    bind(item.references)
        if sources and not spec.summary_references:
            raise ValueError('摘要需要关联引用；缺资料的部分请在coverage和gaps说明')
        if not sources:
            raise ValueError('本轮没有可引用原文，无法生成研究展示页；请先检索或补充授权资料')
        if getattr(ctx, 'page_scene_id', None):
            original = ctx.page_edit_content
            if not original or spec.schema_version != 2:
                raise ValueError('局部修改仅适用于可视化页面')
            before = {s['id']: s for s in stored_spec(original).model_dump()['scenes'] if s['id'] != ctx.page_scene_id}
            after = {s['id']: s for s in content['scenes'] if s['id'] != ctx.page_scene_id}
            for value in after.values():
                value.pop('figure', None)
            if before != after or ctx.page_scene_id not in {s.id for s in spec.scenes}:
                raise ValueError('局部修改不能改变其他场景')
        payload = {'content': content, 'sources': list(sources.values())}
        encoded = kb.json_text(payload)
        if len(encoded) > 250000 or redact(encoded) != encoded:
            raise ValueError('页面内容过大或含疑似凭据，请精简内容')
        return payload
    finally:
        # Validation retries must not consume the conversation figure budget.
        ctx.chart_count = initial_count


class ResearchPages:
    def __init__(self, store):
        self.store = store
        self.directory = store.root / 'reports/pages'

    def path(self, key):
        return self.directory / (ws.safe_id(key) + '.json')

    def get(self, key):
        path = self.path(key)
        if not path.exists():
            raise ValueError('页面不存在或已删除')
        page = kb.read_json(path)
        ws.selected(self.store.root, page['industry'], many=True)
        return page

    @staticmethod
    def brief(page):
        current = page['revisions'][-1]
        return {k: deepcopy(page[k]) for k in ('id', 'industry', 'title', 'tags', 'favorite', 'archived', 'created_at', 'updated_at', 'version')} | {
            'regions': current['content']['regions'], 'summary': current['content']['summary'][:220],
            'visual_preview': next(({'kind': s['kind'], 'title': s['title'],
                'nodes': [{'label': n['label'], 'icon': n['icon']} for n in s.get('nodes', [])[:4]]}
                for s in current['content'].get('scenes', [])), None)}

    def listing(self, industry='all', q='', region='', tag='', archived=False, favorite=False):
        allowed = {e['id'] for e in ws.selected(self.store.root, industry, many=True)}
        if industry == 'all':
            allowed.add('all')
        result = []
        for path in self.directory.glob('*.json'):
            item = self.brief(kb.read_json(path))
            if item['industry'] not in allowed:
                continue
            if industry != 'all' and item['industry'] != industry:
                continue
            if item['archived'] != archived or favorite and not item['favorite']:
                continue
            if region and region not in item['regions'] or tag and tag not in item['tags']:
                continue
            if q and q.casefold() not in (item['title'] + ' ' + ' '.join(item['tags']) + ' ' + item['summary']).casefold():
                continue
            result.append(item)
        return sorted(result, key=lambda p: (p['favorite'], p['updated_at']), reverse=True)

    def check_version(self, page, version):
        if type(version) is not int or page['version'] != version:
            raise ValueError('页面版本已变化，请刷新后重试，未覆盖现有修改')

    def commit(self, industry, payload, prompt, key=None, expected_version=None, operation='create', model='', context=None):
        ws.selected(self.store.root, industry, many=True)
        with kb.lock(self.store.root):
            if context is not None:
                payload = validate_page(context, stored_spec(payload['content']))
            page = self.get(key) if key else None
            if page:
                self.check_version(page, expected_version)
                if page['industry'] != industry:
                    raise ValueError('页面所属档案与当前任务不一致')
            stamp = kb.now()
            page = page or {'id': kb.uuid4().hex, 'industry': industry, 'favorite': False,
                           'archived': False, 'created_at': stamp, 'version': 0, 'revisions': [],
                           'research_request': redact(prompt[:12000])}
            revision = {**deepcopy(payload), 'number': page['version'] + 1, 'created_at': stamp,
                        'operation': operation, 'request': redact(prompt[:12000]), 'model': model}
            page['revisions'].append(revision)
            page.update(title=payload['content']['title'], tags=payload['content']['tags'],
                        updated_at=stamp, version=revision['number'])
            kb.atomic_write(self.path(page['id']), kb.json_text(page))
        return self.brief(page)

    def mutate(self, key, action, version, **changes):
        with kb.lock(self.store.root):
            page = self.get(key)
            self.check_version(page, version)
            if action == 'delete':
                self.path(key).unlink()
                return {'deleted': True}
            if action == 'copy':
                page = deepcopy(page)
                page.update(id=kb.uuid4().hex, title=page['title'] + ' · 副本', favorite=False,
                            archived=False, created_at=kb.now(), updated_at=kb.now(), version=1)
                revision = deepcopy(page['revisions'][-1])
                revision.update(number=1, operation='copy', created_at=kb.now())
                revision['content']['title'] = page['title']
                page['revisions'] = [revision]
            else:
                revision = deepcopy(page['revisions'][-1])
                if action == 'restore':
                    number = changes.get('revision')
                    revision = deepcopy(next((r for r in page['revisions'] if r['number'] == number), None))
                    if not revision:
                        raise ValueError('历史版本不存在')
                    page.update(title=revision['content']['title'], tags=revision['content']['tags'])
                elif action == 'metadata':
                    for field in ('favorite', 'archived'):
                        if field in changes:
                            if type(changes[field]) is not bool:
                                raise ValueError('页面开关须为布尔值')
                            page[field] = changes[field]
                    if 'title' in changes:
                        title = changes['title']
                        if not isinstance(title, str) or not 0 < len(title.strip()) <= 160 or redact(title) != title:
                            raise ValueError('页面名称须为1至160字符且不含凭据')
                        page['title'] = revision['content']['title'] = title.strip()
                else:
                    raise ValueError('未知页面操作')
                page['version'] += 1
                page['updated_at'] = kb.now()
                revision.update(number=page['version'], operation=action, created_at=page['updated_at'])
                page['revisions'].append(revision)
            kb.atomic_write(self.path(page['id']), kb.json_text(page))
            return self.brief(page)

    def warnings(self, revision, industry):
        result = []
        allowed = {e['id'] for e in self.store.research_entries(industry)}
        for source in revision['sources']:
            issue = ''
            try:
                if source['kind'] == 'material':
                    current = Knowledge(self.store).get(source['industry_id'], source['id'])
                    if source['industry_id'] not in allowed or not current.get('model_allowed') or current.get('sensitivity') == 'restricted':
                        issue = '资料模型授权已撤回或档案范围已变化'
                    elif kb.material_fingerprint(current) != source.get('retrieved_fingerprint'):
                        issue = '资料版本已变化'
                elif source['kind'] == 'evidence':
                    current = next((r for r in self.store.evidence(source['industry_id'], limit=10**9) if r['id'] == source['id']), None)
                    if source['industry_id'] not in allowed or not current or not self.store.evidence_model_allowed(current, industry):
                        issue = '引用证据已不可用'
                    elif _evidence_fingerprint(current) != source.get('retrieved_fingerprint'):
                        issue = '引用证据版本已变化'
                elif industry != 'all':
                    from .agent_runtime import allowed_source
                    if not allowed_source(source['url'], self.store.sources(industry)):
                        issue = '网页超出当前限定来源'
            except (ValueError, OSError, KeyError):
                issue = '引用原件已不可用'
            if issue:
                result.append({'ref': source['ref'], 'title': source['title'], 'message': issue})
        return result

    def detail(self, key, revision=None):
        page = self.get(key)
        current = next((r for r in page['revisions'] if r['number'] == revision), None) if revision is not None else page['revisions'][-1]
        if current is None:
            raise ValueError('历史版本不存在')
        return {**self.brief(page), 'revision': current, 'warnings': self.warnings(current, page['industry']),
                'history': [{k: r[k] for k in ('number', 'created_at', 'operation')} for r in reversed(page['revisions'])]}

    def prepare_edit(self, ctx, key, version, refresh=False):
        page = self.get(key)
        self.check_version(page, version)
        if page['industry'] != ctx.industry:
            raise ValueError('修改页面时必须保持原所属档案')
        revision = page['revisions'][-1]
        warnings = self.warnings(revision, ctx.industry)
        if refresh:
            # Refresh never resends obsolete or revoked excerpts to the model.
            # Search again from the user's original request and page organization.
            return kb.json_text({'original_request': page.get('research_request', revision['request']),
                'audience': revision['content']['audience'],
                'regions': revision['content']['regions'],
                'section_kinds': [b['kind'] for b in (revision['content'].get('scenes') or revision['content'].get('blocks', []))],
                'note': '旧引用不作为本轮依据；须重新检索当前授权资料。'})
        if warnings:
            raise ValueError('页面依据版本或授权已变化，请使用“更新研究”重新检索当前授权资料')
        for source in revision['sources']:
            ctx.chart_sources[source['ref']] = deepcopy(source)
            if source['kind'] == 'page':
                ctx.pages[source['url']] = {**source, 'url': source['url']}
            # Apply the same live permission and version checks as new citations.
            if source['kind'] != 'page':
                _source(ctx, source['ref'])
        content = stored_spec(revision['content']).model_dump()
        if getattr(ctx, 'page_scene_id', None):
            if content['schema_version'] != 2 or ctx.page_scene_id not in {s['id'] for s in content['scenes']}:
                raise ValueError('待修改的场景不存在')
            ctx.page_edit_content = deepcopy(content)
        return kb.json_text({'page': content, 'sources': revision['sources']})
