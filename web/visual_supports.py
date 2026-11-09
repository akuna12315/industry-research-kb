"""Server-owned verbatim excerpts; the model selects IDs instead of retyping quotes."""
import hashlib
import re


def source_supports(ctx, ref):
    if not getattr(ctx, 'generate_page', False):
        return []
    from .charts import _source
    source = _source(ctx, ref)
    catalog = ctx.visual_supports
    existing = [entry for entry in catalog.values() if entry['reference'] == ref]
    if not existing:
        text = source.get('text', '')
        for start in range(0, min(len(text), 2200), 540):
            if len(catalog) >= 64:
                break
            quote = text[start:start+600]
            if not quote.strip():
                continue
            key = 'support-' + hashlib.sha256((ref+'\0'+str(start)+'\0'+quote).encode()).hexdigest()[:18]
            entry = {'support_ref':key,'reference':ref,'quote':quote}
            catalog[key] = entry
            existing.append(entry)
            if start+600 >= len(text):
                break
    return [{'support_ref':e['support_ref'],'quote':e['quote'],
             'numbers':list(dict.fromkeys(re.findall(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?',re.sub(r'【[^】]*】','',e['quote']))))[:32]}
            for e in existing]


def bind_support(ctx, item, path):
    if not item.support_ref:
        return
    from .charts import ChartValidationError, _source, _squash, _excerpt_supported
    entry = getattr(ctx, 'visual_supports', {}).get(item.support_ref)
    if entry is None:
        raise ChartValidationError('support_ref必须选择本轮检索返回的摘录编号', 'reference', [*path,'support_ref'])
    source = _source(ctx, entry['reference'])
    if not _excerpt_supported(entry['quote'],source):
        raise ChartValidationError('摘录与资料版本不一致，请重新检索', 'source_changed', [*path,'support_ref'])
    if item.references and entry['reference'] not in item.references:
        raise ChartValidationError('support_ref与references不属于同一原文', 'reference', [*path,'references'])
    if item.quote and _squash(item.quote) not in _squash(entry['quote']):
        raise ChartValidationError('quote不在所选摘录中；使用support_ref时可省略quote', 'quote', [*path,'quote'])
    item.references = item.references or [entry['reference']]
    item.quote = item.quote or entry['quote']
    item.support_ref = ''  # Stored pages contain the resolved text, not a transient token.
