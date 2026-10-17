"""Region-independent professional templates; templates are not factual evidence."""
FORESTRY_TOPICS = ['政策法规与规划', '国土绿化项目与设计', '资源调查与空间资料', '社会经济与林业指标',
                   '技术标准', '成果案例', '生态修复与森林经营', '专业知识与方法']
FORESTRY_QUERIES = {
    '政策法规与规划': ['林业 国土绿化 规划 官方 批复', '科学绿化 实施意见 官方'],
    '国土绿化项目与设计': ['国土绿化 作业设计 可研 批复', '国土绿化 示范项目 实施方案 验收'],
    '资源调查与空间资料': ['第三次全国国土调查 年度变更 林草资源 公开', '造林绿化 空间适宜性评估 公开成果'],
    '社会经济与林业指标': ['统计公报 林业 国土绿化 统计口径'],
    '技术标准': ['GB/T 15776 标准 状态', 'GB/T 26424 标准 状态', 'LY/T 1821 林业地图图式 标准'],
    '成果案例': ['国土绿化 示范项目 成果 验收 官方'],
    '生态修复与森林经营': ['森林可持续经营 项目 管理 办法 官方'],
    '专业知识与方法': ['造林 作业设计 资料依据 林草 官方']}


def regions(scope):
    values = scope.get('research_regions', [scope['primary_region']])
    if not isinstance(values, list) or not values or len(values) > 20 or any(
        not isinstance(r, str) or not r.strip() or len(r) > 100 for r in values):
        raise ValueError('研究地区应为1至20个非空地区名称')
    result = list(dict.fromkeys(r.strip() for r in values))
    if scope['primary_region'].strip() not in result:
        raise ValueError('同时研究的地区须包含主要地区')
    return result


def forestry_queries(scope):
    selected = regions(scope)
    return {topic: ([f'{r} {q}' for r in selected for q in FORESTRY_QUERIES.get(topic, [topic + ' 官方'])]
                    if topic != '技术标准' else list(FORESTRY_QUERIES['技术标准'])) for topic in scope['topics']}


def national_sources():
    return {'mode': 'preferred', 'discovery_domains': [], 'sources': [
        {'id': key, 'name': name, 'url': url, 'type': 'government', 'priority': 1,
         'enabled': True, 'verified': False, 'checked_date': None, 'categories': [],
         'notes': '全国基础入口；新增档案不表示已访问核验。地方入口由用户指定或研究发现。'}
        for key, name, url in [('national-forestry', '国家林业和草原局', 'https://www.forestry.gov.cn/'),
            ('national-standards', '国家标准全文公开系统', 'https://openstd.samr.gov.cn/bzgk/std/')]]}


def sync_queries(home, old, updated, kb):
    if updated.get('specialty') != 'forestry':
        return
    path = home / 'config/queries.json'
    existing = kb.read_json(path) if path.exists() else None
    # Preserve explicit custom queries; only regenerate unchanged template output.
    if existing is None or (old.get('query_template') == 'forestry-v1' and existing == forestry_queries(old)):
        kb.atomic_write(path, kb.json_text(forestry_queries(updated)))
