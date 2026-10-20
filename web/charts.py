"""Bounded research figures backed by sources retrieved in the current turn."""
from datetime import date, datetime
import math
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .knowledge import Knowledge, redact
from .service import kb


class ChartPoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str = Field(min_length=1, max_length=120)
    date: str | None = Field(default=None, max_length=10)
    date_kind: Literal['event', 'published', 'effective', 'unknown'] = 'event'
    value: float | None = Field(default=None, ge=-1e15, le=1e15)
    status: Literal['reported', 'planned', 'uncertain', 'conflict'] = 'reported'
    summary: str = Field(default='', max_length=600)
    quote: str = Field(default='', max_length=1200)
    references: list[str] = Field(min_length=1, max_length=4)

    @field_validator('date')
    @classmethod
    def calendar_date(cls, value):
        if value is not None:
            if not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', value):
                raise ValueError('日期只支持YYYY、YYYY-MM、YYYY-MM-DD或null')
            date.fromisoformat(value + ('-01-01' if len(value) == 4 else '-01' if len(value) == 7 else ''))
        return value

    @field_validator('value', mode='before')
    @classmethod
    def finite_value(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError('数值需为有限数字或null；未知不能填0')
        return value


class ResearchChart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['timeline', 'line', 'bar']
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default='', max_length=800)
    items: list[ChartPoint] = Field(min_length=1, max_length=30)
    metric: str = Field(default='', max_length=160)
    unit: str = Field(default='', max_length=80)
    geography: str = Field(default='', max_length=160)
    scope: str = Field(default='', max_length=300)
    basis: str = Field(default='', max_length=300)
    nature: Literal['actual', 'planned'] = 'actual'
    note: str = Field(default='', max_length=800)


def _remember(ctx, kind, record, snippet=None):
    key = 'chart-' + ctx.chart_namespace + '-' + str(len(ctx.chart_sources) + 1)
    source = {k: record.get(k) for k in ('id', 'industry_id', 'title', 'url', 'checked_date',
              'published_date', 'event_date', 'effective_date', 'status', 'region', 'metrics')}
    source.update(kind=kind, ref=key, text=record.get('summary', '') if kind == 'evidence' else record.get('text', ''))
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
    return [{**r, 'chart_ref': _remember(ctx, 'evidence', r)} for r in records]


def register_materials(ctx, records):
    result = []
    for record in records:
        snippets = [{**s, 'chart_ref': _remember(ctx, 'material', record, s)} for s in record['snippets']]
        result.append({**record, 'snippets': snippets})
    return result


def register_page(ctx, page):
    return {**page, 'chart_ref': _remember(ctx, 'page', page)}


def _source(ctx, ref):
    source = ctx.chart_sources.get(ref)
    if source is None:
        raise ValueError('图表仅能引用本轮工具返回的chart_ref，请先重新检索依据')
    if source['kind'] in ('material', 'evidence'):
        allowed = {e['id'] for e in ctx.store.research_entries(ctx.industry)}
        if source['industry_id'] not in allowed:
            raise ValueError('图表引用超出当前档案及共享基础库')
        if source['kind'] == 'material':
            current = Knowledge(ctx.store).get(source['industry_id'], source['id'])
            if not current.get('model_allowed') or current.get('sensitivity') == 'restricted':
                raise ValueError('图表资料引用权限已撤回')
            if ctx.industry != 'all' and source['industry_id'] != ctx.industry and current.get('sensitivity') != 'public':
                raise ValueError('共享资料需为公开且已授权资料')
    else:
        page = ctx.pages.get(source.get('url'))
        if not page or not ctx.research:
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
    return _date_in_text(point.date, source.get('text'))


def _metric_supported(chart, point, source):
    for metric in source.get('metrics') or []:
        same = all(str(metric.get(key, '')) == str(getattr(chart, attr)) for key, attr in (
            ('name', 'metric'), ('unit', 'unit'), ('geography', 'geography'), ('scope', 'scope'),
            ('basis', 'basis')))
        nature = 'actual' if metric.get('nature') == 'reported_actual' else metric.get('nature')
        same = same and nature == chart.nature
        value = metric.get('value')
        if same and str(metric.get('period')) == point.date and type(value) in (int, float) and math.isfinite(value):
            if point.value is not None and math.isclose(float(value), point.value, rel_tol=1e-12, abs_tol=1e-12):
                return 'registered-metric'
    quote, text = _squash(point.quote), _squash(source.get('text'))
    if not quote or quote not in text or not _date_in_text(point.date, point.quote):
        return None
    # For numbers extracted from a document, require a verbatim quote containing the
    # complete comparison contract. This does not claim semantic or external verification.
    if not all(_squash(getattr(chart, key)) in quote for key in ('metric', 'unit', 'scope', 'basis')):
        return None
    if chart.geography != source.get('region') and _squash(chart.geography) not in quote:
        return None
    if chart.nature == 'planned':
        nature_ok = any(word in quote for word in ('计划', '目标', '预算', '拟'))
    else:
        nature_ok = source.get('status') != 'planned' and any(word in quote for word in ('实际', '实绩', '已完成'))
    if not nature_ok:
        return None
    if point.value is None:
        return 'quoted-missing' if any(word in quote for word in ('未知', '缺失', '未统计', '暂无', '未公布')) else None
    without_dates = re.sub(r'\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?', '', point.quote)
    if len(point.date) == 4:
        without_dates = re.sub(r'(?<!\d)' + point.date + r'(?!\d|\s*' + re.escape(chart.unit) + ')', '', without_dates)
    numbers = re.findall(r'(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])', without_dates.replace(',', ''))
    return 'quoted-extraction' if any(math.isclose(float(n), point.value, rel_tol=1e-12, abs_tol=1e-12) for n in numbers) else None


def validate_chart(ctx, chart):
    if ctx.chart_count >= 3:
        raise ValueError('每轮最多绘制3幅图，请精简图表')
    if chart.kind != 'timeline':
        if not all(getattr(chart, k).strip() for k in ('metric', 'unit', 'geography', 'scope', 'basis')):
            raise ValueError('趋势图需完整提供指标、单位、地区、统计对象和比较口径')
        if len({len(p.date or '') for p in chart.items}) != 1 or any(p.date is None for p in chart.items):
            raise ValueError('趋势图需相同时间粒度；未知期间不能定位到图上')
        if len({p.date for p in chart.items}) != len(chart.items):
            raise ValueError('同一期间不能重复，冲突数值请分开绘图并说明')
        if sum(p.value is not None for p in chart.items) < 2:
            raise ValueError('至少需要两个有依据的数值才能绘制趋势图')
    items = []
    for point in chart.items:
        sources = [_source(ctx, ref) for ref in dict.fromkeys(point.references)]
        if point.quote and not any(_squash(point.quote) in _squash(s.get('text')) for s in sources):
            raise ValueError('原文摘录须逐字来自所引片段，不可补写或拼接为原话')
        if point.date is not None and not any(_date_supported(point, s) or (
                chart.kind != 'timeline' and any(str(m.get('period')) == point.date for m in s.get('metrics') or [])) for s in sources):
            raise ValueError('日期未在所引片段或对应日期字段中出现，不能借用核验/导入日期')
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
            if chart.nature == 'actual' and point.date > datetime.now(kb.TZ).date().isoformat():
                raise ValueError('未来期间不能作为已完成的实际数值')
            if point.date_kind == 'unknown' or point.status in ('uncertain', 'conflict') and point.value is not None:
                raise ValueError('存疑或冲突数值不连成确定趋势，请说明分歧或使用未知值')
            if (point.status == 'planned') != (chart.nature == 'planned'):
                raise ValueError('计划与实际数值不能混在同一趋势图')
            matches = [mode for s in sources if (mode := _metric_supported(chart, point, s))]
            if not matches:
                raise ValueError('数值或比较口径无匹配依据；请引用登记指标，或含日期、指标、单位、对象、口径和性质的原文短摘录')
            item['support'] = matches[0]
        item['sources'] = [{k: s.get(k) for k in ('ref', 'kind', 'id', 'industry_id', 'title', 'url',
                           'checked_date', 'published_date', 'event_date', 'effective_date', 'status', 'citation', 'location')} |
                           {'excerpt': point.quote if point.quote and _squash(point.quote) in _squash(s.get('text')) else s.get('text', '')[:1600]} for s in sources]
        for source in item['sources']:
            url = urlsplit(source.get('url') or '')
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
                source['url'] = ''
        items.append(item)
    items.sort(key=lambda p: (p['date'] is None, p['date'] or ''))
    result = {**chart.model_dump(exclude={'items'}), 'items': items,
              'coverage_note': '仅依据本轮已检索片段整理，不代表完整覆盖；事件、发布与实施日期分开标注。'}
    encoded = kb.json_text(result)
    if len(encoded) > 180000 or redact(encoded) != encoded:
        raise ValueError('图表过大或含疑似凭据，请精简并移除凭据')
    ctx.chart_count += 1
    return result
