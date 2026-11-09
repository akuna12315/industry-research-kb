"""Bounded research figures backed by sources retrieved in the current turn."""
from datetime import date, datetime
import hashlib
import math
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .knowledge import Knowledge, redact
from .service import kb


class ChartValidationError(ValueError):
    def __init__(self, message, code='data', field_path=(), issues=()):
        super().__init__(message)
        self.code = code
        self.field_path = list(field_path)
        self.issues = list(issues)


class ChartPoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str = Field(min_length=1, max_length=120)
    date: str | None = Field(default=None, max_length=10)
    date_kind: Literal['event', 'published', 'effective', 'unknown'] = 'event'
    value: float | None = Field(default=None, ge=-1e15, le=1e15)
    x: float | None = Field(default=None, ge=-1e15, le=1e15)
    status: Literal['reported', 'planned', 'uncertain', 'conflict'] = 'reported'
    summary: str = Field(default='', max_length=600)
    quote: str = Field(default='', max_length=1200)
    support_ref: str = Field(default='', max_length=100, description='优先选择检索结果的support_ref，后端填入逐字quote和references，无需重抄引文。')
    references: list[str] = Field(default_factory=list, max_length=4)

    @field_validator('date')
    @classmethod
    def calendar_date(cls, value):
        if value is not None:
            if not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', value):
                raise ValueError('日期只支持YYYY、YYYY-MM、YYYY-MM-DD或null')
            date.fromisoformat(value + ('-01-01' if len(value) == 4 else '-01' if len(value) == 7 else ''))
        return value

    @field_validator('value', 'x', mode='before')
    @classmethod
    def finite_value(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError('数值需为有限数字或null；未知不能填0')
        return value


class ResearchChart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['timeline', 'line', 'bar', 'pie']
    axis: Literal['time', 'category', 'number'] = 'time'
    period: str = Field(default='', max_length=160)
    x_metric: str = Field(default='', max_length=100)
    x_unit: str = Field(default='', max_length=60)
    whole: str = Field(default='', max_length=160)
    total: float | None = Field(default=None, gt=0, le=1e15)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default='', max_length=800)
    items: list[ChartPoint] = Field(min_length=1, max_length=30)
    metric: str = Field(default='', max_length=160)
    unit: str = Field(default='', max_length=80)
    geography: str = Field(default='', max_length=160)
    scope: str = Field(default='', max_length=300)
    basis: str = Field(default='', max_length=300)
    nature: Literal['actual', 'planned', 'reported'] = 'actual'
    note: str = Field(default='', max_length=800)

    @field_validator('total', mode='before')
    @classmethod
    def finite_total(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError('整体总量须为有限正数')
        return value


def _remember(ctx, kind, record, snippet=None):
    key = 'chart-' + ctx.chart_namespace + '-' + str(len(ctx.chart_sources) + 1)
    source = {k: record.get(k) for k in ('id', 'industry_id', 'title', 'url', 'checked_date',
              'published_date', 'event_date', 'effective_date', 'status', 'region', 'metrics', 'source_kind', 'related_material')}
    source.update(kind=kind, ref=key, text=record.get('summary', '') if kind == 'evidence' else record.get('text', ''))
    if kind == 'material':
        source['retrieved_fingerprint'] = record.get('source_fingerprint') or kb.material_fingerprint(record)
    elif kind == 'evidence':
        source['retrieved_fingerprint'] = _evidence_fingerprint(record)
    if snippet:
        source.update(text=snippet['text'], citation=snippet['citation'], location=snippet.get('location', ''))
    for prior in ctx.chart_sources.values():
        if all(prior.get(k) == value for k, value in source.items() if k != 'ref'):
            return prior['ref']
    if len(ctx.chart_sources) >= 120:
        return ''  # Preserve ordinary search even when the figure-source budget is exhausted.
    ctx.chart_sources[key] = source
    return key


def register_evidence(ctx, records):
    result = []
    from .visual_supports import source_supports
    for record in records:
        ref = _remember(ctx, 'evidence', record)
        result.append({**record,'chart_ref':ref})
        if getattr(ctx,'generate_page',False):
            result[-1]['visual_supports']=source_supports(ctx,ref) if ref else []
    return result


def _evidence_fingerprint(record):
    fields = ('title', 'summary', 'url', 'checked_date', 'status', 'metrics', 'material_source', 'related_material')
    return hashlib.sha256(kb.json_text({key: record.get(key) for key in fields}).encode()).hexdigest()


def register_materials(ctx, records):
    result = []
    from .visual_supports import source_supports
    for record in records:
        snippets = [{**s, 'chart_ref': _remember(ctx, 'material', record, s)} for s in record['snippets']]
        for snippet in snippets:
            if snippet['chart_ref'] and getattr(ctx,'generate_page',False):
                snippet['visual_supports'] = source_supports(ctx,snippet['chart_ref'])
        result.append({**record, 'snippets': snippets})
    return result


def register_page(ctx, page):
    return {**page, 'chart_ref': _remember(ctx, 'page', page)}


def _source(ctx, ref):
    source = ctx.chart_sources.get(ref)
    if source is None:
        raise ChartValidationError('图表仅能引用本轮工具返回的chart_ref，请先重新检索依据', 'reference')
    selected = getattr(ctx, 'page_materials', [])
    if selected and (source['kind'] != 'material' or not any(
            s['industry_id'] == source['industry_id'] and s['id'] == source['id'] for s in selected)):
        raise ChartValidationError('页面只能引用选定资料的原文片段，请重新检索已选资料', 'reference')
    if source['kind'] in ('material', 'evidence'):
        allowed = {e['id'] for e in ctx.store.research_entries(ctx.industry)}
        if source['industry_id'] not in allowed:
            raise ValueError('图表引用超出所选行业范围')
        if source['kind'] == 'material':
            current = Knowledge(ctx.store).get(source['industry_id'], source['id'])
            if not current.get('model_allowed') or current.get('sensitivity') == 'restricted':
                raise ValueError('图表资料引用权限已撤回')
            if ctx.industry != 'all' and source['industry_id'] != ctx.industry and current.get('sensitivity') != 'public':
                raise ValueError('跨行业资料需为公开且已授权资料')
            if source.get('retrieved_fingerprint') != kb.material_fingerprint(current):
                raise ValueError('原始资料版本已变化，请重新检索后绘图')
        elif source.get('source_kind') == 'local_material' or source.get('related_material'):
            current = next((r for r in ctx.store.evidence(source['industry_id'], limit=10**9)
                            if r['id'] == source['id']), None)
            if current is None or not ctx.store.evidence_model_allowed(current, ctx.industry):
                raise ValueError('证据原始资料权限或版本已变化，请重新检索和核验')
            if source.get('retrieved_fingerprint') != _evidence_fingerprint(current):
                raise ValueError('证据版本已变化，请重新检索后绘图')
    else:
        page = ctx.pages.get(source.get('url'))
        if not page or not (ctx.research or getattr(ctx, 'page_operation', '') == 'layout'):
            raise ValueError('公开网页需在本轮研究中读取原文后才能用于绘图')
        from .agent_runtime import allowed_source
        if not allowed_source(source['url'], ctx.store.sources(ctx.industry)):
            raise ValueError('网页已超出当前限定信源')
    return source


def _squash(text):
    return re.sub(r'\s+', '', text or '')


def _date_in_text(value, text):
    pieces = value.split('-')
    # Bound year-only matches: 2025 must not match an identifier such as 120253.
    pattern = r'(?<!\d)' + pieces[0]
    for number, separator in zip(pieces[1:], ('年', '月')):
        pattern += r'(?:-|/|\.|' + separator + r')\s*0?' + str(int(number))
    return bool(re.search(pattern + r'(?!\d)', text or ''))


def _date_supported(point, source):
    if point.date is None:
        return True
    field = {'event': 'event_date', 'published': 'published_date', 'effective': 'effective_date'}.get(point.date_kind)
    if field is None:
        return False
    known = source.get(field)
    if isinstance(known, str) and (known == point.date or known.startswith(point.date + '-')):
        return True
    # Excerpts may contain event/publication/effective dates that have not been catalogued.
    # checked_date is deliberately never used as an event date.
    return any(_date_in_text(point.date, text) for text in [source.get('text', ''), *source.get('support_quotes', [])])


def _excerpt_supported(quote, source):
    return bool(_squash(quote)) and any(_squash(quote) in _squash(text)
        for text in [source.get('text', ''), *source.get('support_quotes', [])])


def _number_in_text(value, text):
    numbers = re.findall(r'(?<![\d.])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\d.])', text.replace(',', '').replace('−', '-'))
    return value is not None and any(math.isclose(float(n), value, rel_tol=1e-12, abs_tol=1e-12) for n in numbers)


def _has_plan_language(text):
    # Fitting and objective functions are common research terms, not future plans.
    return bool(re.search(r'计划|预算|拟(?:于|在|将|建|开展|实施|完成|达到|新增|定于|投资|投入|投产|扩建|采购|发布|发射)|目标(?!函数|检测|识别|对象)', text))


def _quote_issues(chart, point, source):
    quote = _squash(point.quote)
    if not _excerpt_supported(point.quote, source):
        return ['quote']
    issues = []
    if chart.axis == 'time' and not _date_in_text(point.date, point.quote):
        issues.append('date')
    if chart.axis != 'time' and _squash(chart.period) not in quote:
        issues.append('period')
    if chart.axis == 'category' and _squash(point.label) not in quote:
        issues.append('label')
    if chart.axis == 'number':
        if not _number_in_text(point.x, point.quote):
            issues.append('x')
        issues.extend(k for k in ('x_metric', 'x_unit') if _squash(getattr(chart, k)) not in quote)
    issues.extend(k for k in ('metric', 'unit', 'scope', 'basis') if _squash(getattr(chart, k)) not in quote)
    if chart.geography != source.get('region') and _squash(chart.geography) not in quote:
        issues.append('geography')
    if chart.nature == 'planned':
        nature_ok = _has_plan_language(quote)
    elif chart.nature == 'actual':
        nature_ok = source.get('status') != 'planned' and any(word in quote for word in ('实际', '实绩', '已完成'))
    else:
        nature_ok = source.get('status') != 'planned' and not _has_plan_language(quote)
    if not nature_ok:
        issues.append('nature')
    without_dates = re.sub(r'\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?', '', point.quote)
    if point.date and len(point.date) == 4:
        without_dates = re.sub(r'(?<!\d)' + point.date + r'(?!\d|\s*' + re.escape(chart.unit) + ')', '', without_dates)
    if point.value is None:
        if not any(word in quote for word in ('未知', '缺失', '未统计', '暂无', '未公布')):
            issues.append('value')
    elif not _number_in_text(point.value, without_dates):
        issues.append('value')
    return issues


def _metric_supported(chart, point, source):
    period = point.date if chart.axis == 'time' else chart.period
    for metric in source.get('metrics') or []:
        same = all(str(metric.get(key, '')) == str(getattr(chart, attr)) for key, attr in (
            ('name', 'metric'), ('unit', 'unit'), ('geography', 'geography'), ('scope', 'scope'),
            ('basis', 'basis')))
        nature = 'actual' if metric.get('nature') == 'reported_actual' else metric.get('nature')
        same = same and (nature == chart.nature or chart.nature == 'reported' and nature == 'actual')
        if chart.axis == 'category':
            same = same and point.label in (str(metric.get('category', '')), source.get('title'))
        if chart.axis == 'number':
            same = same and metric.get('x') == point.x
        value = metric.get('value')
        if same and str(metric.get('period')) == period and type(value) in (int, float) and math.isfinite(value):
            if point.value is not None and math.isclose(float(value), point.value, rel_tol=1e-12, abs_tol=1e-12):
                return 'registered-metric'
    if _quote_issues(chart, point, source):
        return None
    return 'quoted-missing' if point.value is None else 'quoted-extraction'


def validate_chart(ctx, chart):
    try:
        return _validate_chart(ctx, chart)
    except ChartValidationError:
        raise
    except ValueError as exc:
        message = str(exc)
        code = 'source_changed' if any(k in message for k in ('授权', '撤回', '版本', '档案')) else 'data'
        field = next((field for word, field in [('总量', 'total'), ('合计', 'total'), ('整体', 'whole'),
            ('日期', 'items'), ('期间', 'period'), ('类别', 'axis'), ('x_', 'x_metric'), ('指标', 'metric')]
            if word in message), 'items')
        raise ChartValidationError(message, code, ['chart', field]) from None


def _validate_chart(ctx, chart):
    from .visual_supports import bind_support
    for index, point in enumerate(chart.items):
        bind_support(ctx,point,['chart','items',index])
        if not point.references:
            raise ChartValidationError('每个数据点需要有效support_ref或references', 'reference', ['chart','items',index,'references'])
    if ctx.chart_count >= 3:
        raise ValueError('每轮最多绘制3幅图，请精简图表')
    if chart.kind != 'timeline':
        if not all(getattr(chart, k).strip() for k in ('metric', 'unit', 'geography', 'scope', 'basis')):
            raise ValueError('趋势图需完整提供指标、单位、地区、统计对象和比较口径')
        if chart.axis == 'time':
            if chart.kind == 'pie':
                raise ValueError('饼图使用category轴，不能把不同时期当组成')
            if len({len(p.date or '') for p in chart.items}) != 1 or any(p.date is None for p in chart.items):
                raise ValueError('趋势图需相同时间粒度；未知期间不能定位到图上')
            if len({p.date for p in chart.items}) != len(chart.items):
                raise ValueError('同一期间不能重复，冲突数值请分开绘图并说明')
        else:
            if not chart.period.strip() or any(p.date is not None for p in chart.items):
                raise ValueError('类别/数值轴须提供共同period工况或期间，各点date设null')
            if chart.nature != 'planned' and re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', chart.period) and chart.period > datetime.now(kb.TZ).date().isoformat():
                raise ValueError('未来期间不能作为已完成或报告的数值')
            if chart.axis == 'category':
                if chart.kind not in ('bar', 'pie') or len({p.label for p in chart.items}) != len(chart.items):
                    raise ValueError('类别轴只用于柱状图或饼图，类别不能重复')
            elif chart.kind != 'line' or not chart.x_metric.strip() or not chart.x_unit.strip() or any(p.x is None for p in chart.items) or len({p.x for p in chart.items}) != len(chart.items):
                raise ValueError('数值折线需真实且不重复的x、x_metric与x_unit')
        if sum(p.value is not None for p in chart.items) < 2:
            raise ValueError('至少需要两个有依据的数值才能绘制趋势图')
        if chart.kind == 'pie':
            if not chart.whole.strip() or chart.total is None or not 2 <= len(chart.items) <= 8:
                raise ValueError('饼图须声明整体whole、总量total及2至8项互斥且完整的组成')
            if any(p.value is None or p.value < 0 for p in chart.items):
                raise ValueError('饼图组成不能缺失或为负数')
            percent = chart.unit in ('%', '％', '百分比')
            if percent and chart.total != 100:
                raise ValueError('百分比饼图的total须为100')
            if not math.isclose(sum(p.value for p in chart.items), chart.total, rel_tol=1e-9,
                                abs_tol=.100001 if percent else 1e-9):
                raise ValueError('饼图组成合计须匹配整体总量，不能删除未覆盖部分后重新归一化')
            if not all(_squash(chart.whole) in _squash(p.quote) for p in chart.items):
                raise ValueError('饼图各项须引用同一整体whole的原文')
            if not percent and not any(_number_in_text(chart.total, p.quote) and any(
                    marker in p.quote for marker in ('合计', '总计', '总量', '总数', '共')) for p in chart.items):
                raise ValueError('饼图整体总量须有原文依据')
    items = []
    for index, point in enumerate(chart.items):
        sources = []
        for ref_index, ref in enumerate(point.references):
            try:
                sources.append(_source(ctx, ref))
            except ValueError as exc:
                code = getattr(exc, 'code', 'source_changed')
                raise ChartValidationError(str(exc), code, ['chart', 'items', index, 'references', ref_index]) from None
        if point.quote and not any(_excerpt_supported(point.quote, s) for s in sources):
            raise ChartValidationError('原文摘录须逐字来自所引片段，不可补写或拼接为原话', 'quote', ['chart', 'items', index, 'quote'])
        if point.date is not None and not any(_date_supported(point, s) or (
                chart.kind != 'timeline' and any(str(m.get('period')) == point.date for m in s.get('metrics') or [])) for s in sources):
            raise ChartValidationError('日期未在所引片段或对应日期字段中出现，不能借用核验/导入日期', 'data', ['chart', 'items', index, 'date'])
        item = point.model_dump(exclude={'references'})
        if chart.kind == 'timeline':
            if point.value is not None:
                raise ValueError('事件时间轴不绘制数值，请使用折线图或柱状图')
            if point.date is None:
                item['date_kind'] = 'unknown'
            if point.status == 'reported' and (all(s.get('status') == 'planned' for s in sources) or
                    point.date and point.date > datetime.now(kb.TZ).date().isoformat()):
                raise ValueError('计划来源或未来节点须标记计划/存疑，不能展示为已发生的实绩')
        else:
            if chart.nature in ('actual', 'reported') and point.date and point.date > datetime.now(kb.TZ).date().isoformat():
                raise ValueError('未来期间不能作为已完成的实际数值')
            if chart.axis == 'time' and point.date_kind == 'unknown' or point.status in ('uncertain', 'conflict') and point.value is not None:
                raise ValueError('存疑或冲突数值不连成确定趋势，请说明分歧或使用未知值')
            if (point.status == 'planned') != (chart.nature == 'planned'):
                raise ValueError('计划与实际数值不能混在同一趋势图')
            matches = [mode for s in sources if (mode := _metric_supported(chart, point, s))]
            if not matches:
                issues = min((_quote_issues(chart, point, s) for s in sources), key=len)
                point_fields = {'quote', 'date', 'label', 'value', 'x'}
                paths = [['chart', 'items', index, k] if k in point_fields else ['chart', k] for k in issues]
                raise ChartValidationError('数值或比较口径无匹配依据；需核对逐字quote中的对应字段', 'data',
                                           paths[0] if paths else ['chart', 'items', index], paths)
            item['support'] = matches[0]
            if chart.kind == 'pie':
                item['share'] = 100 * point.value / chart.total
        item['sources'] = [{k: s.get(k) for k in ('ref', 'kind', 'id', 'industry_id', 'title', 'url',
                           'checked_date', 'published_date', 'event_date', 'effective_date', 'status', 'citation', 'location')} |
                           {'excerpt': point.quote if point.quote and _excerpt_supported(point.quote, s) else s.get('text', '')[:1600]} for s in sources]
        for source in item['sources']:
            url = urlsplit(source.get('url') or '')
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
                source['url'] = ''
        items.append(item)
    if chart.axis == 'time':
        items.sort(key=lambda p: (p['date'] is None, p['date'] or ''))
    elif chart.axis == 'number':
        items.sort(key=lambda p: p['x'])
    result = {**chart.model_dump(exclude={'items'}), 'items': items,
              'coverage_note': '仅依据本轮已检索片段整理，不代表完整覆盖；事件、发布与实施日期分开标注。'}
    encoded = kb.json_text(result)
    if len(encoded) > 180000 or redact(encoded) != encoded:
        raise ValueError('图表过大或含疑似凭据，请精简并移除凭据')
    ctx.chart_count += 1
    return result
