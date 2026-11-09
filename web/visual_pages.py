"""Declarative visual scenes. The model supplies meaning, never executable code."""
from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field

from .charts import ResearchChart, _source, _squash, _number_in_text

Icon = Literal['document', 'battery', 'energy', 'health', 'layers', 'workflow', 'database',
               'server', 'screen', 'sensor', 'leaf', 'tree', 'policy', 'map', 'target',
               'experiment', 'chart', 'clock', 'people', 'idea', 'search', 'check']
Kind = Literal['flow', 'architecture', 'concept', 'comparison', 'chart', 'table']
Identifier = str


class VisualModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SourcedVisual(VisualModel):
    references: list[str] = Field(default_factory=list, max_length=4)
    nature: Literal['reported', 'planned', 'inference', 'unknown'] = 'reported'
    quote: str = Field(default='', max_length=600)
    support_ref: str = Field(default='', max_length=100, description='优先选择检索返回的support_ref，后端填入逐字quote及references，避免重抄原话。')


class VisualNode(SourcedVisual):
    id: Identifier = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,47}$')
    label: str = Field(min_length=1, max_length=70)
    detail: str = Field(default='', max_length=700)
    icon: Icon = 'document'
    group: str = Field(default='', max_length=60)


class VisualEdge(SourcedVisual):
    source: str = Field(max_length=48)
    target: str = Field(max_length=48)
    label: str = Field(min_length=1, max_length=40)
    relation: Literal['sequence', 'contains', 'depends', 'input', 'output', 'related'] = 'related'


class VisualDimension(VisualModel):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,47}$')
    label: str = Field(min_length=1, max_length=60)


class VisualCell(SourcedVisual):
    node: str = Field(max_length=48)
    dimension: str = Field(max_length=48)
    text: str = Field(min_length=1, max_length=350)


class SceneOutline(VisualModel):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,47}$')
    kind: Kind
    title: str = Field(min_length=1, max_length=120)


class VisualScene(SceneOutline):
    lead: str = Field(default='', max_length=200)
    direction: Literal['horizontal', 'vertical'] = 'horizontal'
    emphasis: Literal['primary', 'normal'] = 'normal'
    nodes: list[VisualNode] = Field(default_factory=list, max_length=12)
    edges: list[VisualEdge] = Field(default_factory=list, max_length=20)
    dimensions: list[VisualDimension] = Field(default_factory=list, max_length=6)
    cells: list[VisualCell] = Field(default_factory=list, max_length=72)
    # Reuse the existing numeric, units, dates, and source-support checks.
    chart: ResearchChart | None = None


class VisualPlan(VisualModel):
    title: str = Field(min_length=1, max_length=160)
    subtitle: str = Field(default='', max_length=400)
    question: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1500)
    summary_references: list[str] = Field(min_length=1, max_length=8)
    regions: list[str] = Field(default_factory=list, max_length=10)
    tags: list[str] = Field(default_factory=list, max_length=10)
    audience: str = Field(default='一般读者', max_length=160)
    coverage: str = Field(min_length=1, max_length=1500)
    gaps: list[str] = Field(default_factory=list, max_length=15)
    theme: Literal['sage', 'ink', 'sand'] = 'sage'
    layout: Literal['story', 'overview'] = 'story'
    scenes: list[SceneOutline] = Field(min_length=1, max_length=8)


def unique_ids(items, name):
    ids = [item.id for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError(name + '的id不能重复')
    return set(ids)


class SceneValidationError(ValueError):
    def __init__(self, message, field_path, code=None):
        super().__init__(message)
        self.field_path = field_path
        self.code = code


def validate_scene(ctx, scene, bind):
    """Validate structure, permissions and exact supporting quotes, without scoring prose."""
    def support(item, path):
        from .visual_supports import bind_support
        bind_support(ctx,item,path)
        try:
            bind(item.references)
        except ValueError as exc:
            raise SceneValidationError(str(exc), [*path, 'references'], getattr(exc,'code',None)) from None
        if item.nature in ('reported', 'planned'):
            if not item.references or not _squash(item.quote):
                raise SceneValidationError('图形节点、关系和比较内容的来源记载/计划必须引用原文并提供逐字quote；推断须明确标为inference', [*path, 'quote'])
        def quoted(ref):
            source = _source(ctx, ref)
            excerpts = [source.get('text', '')]
            if getattr(ctx, 'page_operation', '') == 'layout' or getattr(ctx, 'page_resuming', False):
                excerpts += source.get('support_quotes', [])
            return any(_squash(item.quote) in _squash(text) for text in excerpts)
        if item.quote and (not item.references or not any(quoted(ref) for ref in item.references)):
            raise SceneValidationError('图形quote必须逐字出现在所引用的原文片段中', [*path, 'quote'])

    ids = unique_ids(scene.nodes, '节点')
    dimensions = unique_ids(scene.dimensions, '比较维度')
    if scene.kind == 'chart':
        if scene.chart is None or scene.nodes or scene.edges or scene.dimensions or scene.cells:
            raise ValueError('chart场景仅接受chart数据')
        return
    if scene.chart is not None or not scene.nodes:
        raise ValueError('示意图需要nodes，数值图表请使用chart场景')
    for index, node in enumerate(scene.nodes):
        support(node, ['nodes', index])
    seen_edges = set()
    for index, edge in enumerate(scene.edges):
        if edge.source not in ids or edge.target not in ids or edge.source == edge.target:
            raise ValueError('关系必须连接两个存在且不同的节点')
        pair = (edge.source, edge.target, edge.relation)
        if pair in seen_edges:
            raise ValueError('同类节点关系不能重复')
        seen_edges.add(pair)
        support(edge, ['edges', index])
    if scene.kind in ('comparison', 'table'):
        if not (2 <= len(scene.nodes) <= 4 if scene.kind == 'comparison' else 1 <= len(scene.nodes) <= 12) or not dimensions or scene.edges:
            raise SceneValidationError('comparison需要2至4个选项；table需要1至12行；均需dimensions且不接受edges', ['nodes'], 'table_shape')
        cells = [(cell.node, cell.dimension) for cell in scene.cells]
        if len(cells) != len(set(cells)) or set(cells) != {(n, d) for n in ids for d in dimensions}:
            raise SceneValidationError('每个选项的每个维度都需要且只能有一个cell；缺失标unknown', ['cells'], 'table_shape')
        for index, cell in enumerate(scene.cells):
            support(cell, ['cells', index])
            if scene.kind == 'table':
                numbers = re.findall(r'(?<![\d.])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\d.])', cell.text.replace(',', '').replace('−', '-'))
                if numbers and (not cell.quote or not cell.references or not all(_number_in_text(float(n), cell.quote) for n in numbers)):
                    raise SceneValidationError('表格数值须逐字得到quote支持，不能把猜测数值写成推断', ['cells', index, 'text'], 'data')
    elif scene.dimensions or scene.cells:
        raise ValueError('只有comparison和table使用dimensions和cells')
    elif len(scene.nodes) > 1 and not scene.edges:
        raise ValueError('多个图形节点需要明确关系，不生成孤立的文字卡片')
    # Flow steps are an ordered DAG; cycles belong in architecture/concept views.
    if scene.kind == 'flow':
        pending = set(ids)
        while pending:
            roots = {key for key in pending if not any(e.target == key and e.source in pending for e in scene.edges)}
            if not roots:
                raise ValueError('流程图不能包含循环；循环关系请用architecture或concept')
            pending -= roots
