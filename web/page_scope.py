"""Explicit material scope for page tasks, checked before retrieval and citation."""
import re
from .knowledge import Knowledge


def personal_material_request(prompt):
    return bool(re.search(r'我的|这份|这篇|该论文|该资料|刚才上传|毕设|毕业设计|毕业论文', prompt))


def requested_scene_limit(prompt):
    match = re.search(r'(?:做|生成|制作|只要|仅需|包含)\s*([1-8一二两三四五六七八])\s*个(?:视图|场景|图)',prompt)
    if not match:
        return None
    value=match.group(1)
    return int(value) if value.isdigit() else {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8}[value]


def validate_material_scope(store, industry, selected):
    allowed = {entry['id'] for entry in store.research_entries(industry)}
    result = []
    for target in selected:
        if not isinstance(target, dict) or set(target) != {'industry_id', 'id'}:
            raise ValueError('页面资料必须包含industry_id和id')
        key = target['industry_id'], target['id']
        if key[0] not in allowed:
            raise ValueError('页面资料超出所选行业范围')
        record = Knowledge(store).get(*key)
        if (not record.get('model_allowed') or record.get('sensitivity') == 'restricted'
                or industry != 'all' and key[0] != industry and record.get('sensitivity') != 'public'):
            raise ValueError('页面资料没有有效模型授权')
        if target not in result:
            result.append(dict(target))
            if len(result) > 6:
                raise ValueError('每页最多选择6份资料')
    return result


def resolve_material_scope(store, industry, prompt, selected=()):
    if selected:
        return validate_material_scope(store, industry, selected)
    records = Knowledge(store).list(industry, model_only=True)
    explicit = [r for r in records if r['id'] in prompt or r['title'] in prompt]
    if explicit:
        return validate_material_scope(store, industry, [
            {'industry_id': r['industry_id'], 'id': r['id']} for r in explicit])
    if records and personal_material_request(prompt):
        raise ValueError('请先选择要生成页面的资料，或在要求中写明完整资料名；不会猜测“我的毕设”指哪一份')
    return []
