"""Read-only, complete source plans for material evidence extraction.

The persisted DOCX index remains the citation authority.  Re-parsing a DOCX only
adds structure: every generated span is checked against that existing index.
No document text, task state, credentials or model output is written here.
"""
from collections import defaultdict
import hashlib
import json
import re

from .docx_index import extract_docx, index_records
from .knowledge import Knowledge
from .service import kb

PARSER_VERSION = 'full-material-source-v1'
TARGET_CHARACTERS = 5500
MAX_CHARACTERS = 7000
MAX_SPLIT_CONTEXT_CHARACTERS = 8000
MAX_SPLIT_GEOMETRY_CHARACTERS = 64000
_MARKER = re.compile(r'(?:\A|\n\n)【((?:正文|脚注|尾注|页眉页脚 [^\n】]+)·(?:表\d+·行\d+·单元格\d+·)?段落\d+)】\n')
_NOTE = re.compile(r'^(?:注(?:\d+)?|备注|说明|单位|数据来源|资料来源)\s*[:：]')
_CAPTION = re.compile(r'^(?:表\s*[\d一二三四五六七八九十]+|附表|续表|单位\s*[:：])')


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _citation(material_id, block, start, end):
    prefix = f'{material_id}#block-{block}-chars-' if block else f'{material_id}#chars-'
    return f'{prefix}{start + 1}-{end}'


def _span(material_id, block, body, start, end, metadata):
    citation = _citation(material_id, block, start, end)
    item = {k: v for k, v in metadata.items() if k not in ('start', 'end', 'text')}
    item.update(citation=citation, text=body[start:end], location=metadata.get('location', '正文'),
                block=block, start=start, end=end, unit_id='unit-' + _digest(citation)[:24])
    item['sha256'] = _digest(item['text'])
    return item


def _fallback_units(part):
    """Locate legacy markers without counting generated locators/separators."""
    body = part['text']
    markers = list(_MARKER.finditer(body))
    if not markers:
        return [{'start': 0, 'end': len(body), 'location': part.get('first_location', '正文'),
                 'kind': 'paragraph'}] if body else []
    if markers[0].start() != 0:
        raise ValueError('DOCX正文索引开头存在无法定位的文字，请重建原文索引')
    result = []
    for number, match in enumerate(markers):
        end = markers[number + 1].start() if number + 1 < len(markers) else len(body)
        location = match.group(1)
        metadata = {'start': match.end(), 'end': end, 'location': location, 'kind': 'paragraph'}
        table = re.search(r'^(.*?)·表(\d+)·行(\d+)·单元格(\d+)·', location)
        if table:
            label, table_number, row, cell = table.groups()
            metadata.update(kind='table_cell', table_id=f'{label}:table-{table_number}',
                            table_row=int(row), table_cell=int(cell))
        result.append(metadata)
    return result


def _docx_structure(knowledge, industry, material, stored_parts):
    """Use the original attachment when available, without changing its index."""
    attachment = material.get('attachment') or {}
    if attachment.get('suffix') != '.docx':
        return None, {}, ['索引未关联DOCX原附件，无法补充原始合并单元格和标题样式。']
    path, _ = knowledge.attachment(industry, material['id'])
    if not path.is_file():
        raise ValueError('DOCX原附件不存在，无法核对全文索引与资料版本')
    expected_hash = attachment.get('sha256')
    if expected_hash:
        hasher = hashlib.sha256()
        with path.open('rb') as handle:
            while piece := handle.read(1_048_576):
                hasher.update(piece)
        if hasher.hexdigest() != expected_hash:
            raise ValueError('DOCX原附件指纹与资料记录不一致，请重新上传并核验')
    blocks, stats = extract_docx(path, include_structure=True)
    regenerated = index_records(blocks)
    if len(regenerated) != len(stored_parts) or any(a['text'] != b['text'] for a, b in zip(regenerated, stored_parts)):
        raise ValueError('DOCX原附件与现有正文索引无法逐字对齐，请重建原文索引后重新核验')
    shapes = {table['id']: table for table in stats.pop('table_structure', [])}
    return regenerated, shapes, []


def _paragraph_units(material):
    body = material.get('content') or material.get('summary', '')
    if not body:
        raise ValueError('资料没有可提取的文字；图片或扫描件需先建立可核对的文字内容')
    # Boundaries include their separators so every stored content character is
    # counted once, including trailing text and blank lines.
    starts = [0] + [m.end() for m in re.finditer(r'\n\s*\n', body) if m.end() < len(body)]
    units = []
    for number, start in enumerate(starts):
        end = starts[number + 1] if number + 1 < len(starts) else len(body)
        units.append(_span(material['id'], None, body, start, end,
                           {'location': f'正文·文字段{number + 1}', 'kind': 'paragraph'}))
    return units


def _split_source(source, maximum=MAX_CHARACTERS):
    """Split unusually long units, keeping exact single-index citations."""
    if len(source['text']) <= maximum:
        return [source]
    result = []
    offset = 0
    while offset < len(source['text']):
        stop = min(len(source['text']), offset + maximum)
        if stop < len(source['text']):
            # Prefer a sentence/line end, but do not strand a tiny prefix.
            choices = [source['text'].rfind(separator, offset + maximum // 2, stop)
                       for separator in ('\n', '。', '；', '！', '？')]
            boundary = max(choices)
            if boundary >= 0:
                stop = boundary + 1
        item = dict(source)
        item.update(start=source['start'] + offset, end=source['start'] + stop,
                    text=source['text'][offset:stop])
        item['citation'] = _citation(source['citation'].split('#', 1)[0], source['block'], item['start'], item['end'])
        item['unit_id'] = 'unit-' + _digest(item['citation'])[:24]
        item['sha256'] = _digest(item['text'])
        result.append(item)
        offset = stop
    return result


def _groups(units):
    """Keep a table row intact unless the row itself exceeds the input bound."""
    groups = []
    current = []
    key = None
    for source in units:
        item_key = (source.get('table_id'), source.get('table_row')) if source.get('table_id') else None
        if current and (not item_key or item_key != key):
            groups.append(current)
            current = []
        current.append(source)
        key = item_key
        if not item_key:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    bounded = []
    for group in groups:
        if sum(len(item['text']) for item in group) <= MAX_CHARACTERS:
            bounded.append(group)
            continue
        row_part = []
        count = 0
        for item in group:
            for piece in _split_source(item):
                if row_part and count + len(piece['text']) > MAX_CHARACTERS:
                    bounded.append(row_part)
                    row_part, count = [], 0
                row_part.append(piece)
                count += len(piece['text'])
        if row_part:
            bounded.append(row_part)
    return bounded


def _table_context(units):
    tables = defaultdict(list)
    positions = defaultdict(list)
    for position, source in enumerate(units):
        if source.get('table_id'):
            tables[source['table_id']].append(source)
            positions[source['table_id']].append(position)
    result = {}
    for table, cells in tables.items():
        first_row = min(cell.get('table_row', 1) for cell in cells)
        headers = [cell for cell in cells if cell.get('table_header')]
        if not headers:
            # A fallback is explicitly labelled as such. Merged top cells can
            # form a multi-level header, so retain the following row too.
            rows = {first_row}
            if any(cell.get('column_span', 1) > 1 or cell.get('vertical_merge') == 'restart'
                   for cell in cells if cell.get('table_row') == first_row):
                rows.add(first_row + 1)
            headers = [cell for cell in cells if cell.get('table_row') in rows]
        context = [dict(cell, context_reason='表头原文' if cell.get('table_header') else '表头候选（未标记表头，保留首行或合并首两行）') for cell in headers]
        begin, end = min(positions[table]), max(positions[table])
        for previous in units[max(0, begin - 3):begin]:
            if not previous.get('table_id') and (len(previous['text']) <= 200 or _CAPTION.match(previous['text'])):
                context.append(dict(previous, context_reason='表前相邻标题或单位原文'))
        for following in units[end + 1:end + 5]:
            if following.get('table_id') or not _NOTE.match(following['text'].lstrip()):
                break
            context.append(dict(following, context_reason='表后注释原文'))
        result[table] = [dict(item, context_table_id=table) for item in context]
    return result


def _hydrated_source(source):
    """Normalize a hydrated citation without requiring original plan fields."""
    if not isinstance(source, dict) or not isinstance(source.get('text'), str) or not source['text']:
        raise ValueError('拆分需要带完整原文的非空引用')
    match = re.fullmatch(r'(.+)#(?:block-(\d+)-)?chars-(\d+)-(\d+)', source.get('citation', ''))
    if not match:
        raise ValueError('拆分原文引用格式无效')
    material, block, first, last = match.groups()
    first, last = int(first), int(last)
    if first < 1 or first > last or last - first + 1 != len(source['text']):
        raise ValueError('拆分原文引用长度与逐字内容不一致')
    result = dict(source, block=int(block) if block else None, start=first - 1, end=last)
    result.setdefault('unit_id', 'unit-' + _digest(result['citation'])[:24])
    result['sha256'] = _digest(result['text'])
    return result


def _sliced_source(source, start, end):
    item = dict(source, start=source['start'] + start, end=source['start'] + end,
                text=source['text'][start:end])
    material = source['citation'].split('#', 1)[0]
    item['citation'] = _citation(material, source['block'], item['start'], item['end'])
    item['unit_id'] = 'unit-' + _digest(item['citation'])[:24]
    item['sha256'] = _digest(item['text'])
    if 'text_hash' in source:
        item['text_hash'] = hashlib.sha256(kb.json_text(item['text']).encode()).hexdigest()
    return item


def _columns_overlap(first, second):
    left = first.get('column')
    right = second.get('column')
    if left is None or right is None:
        return True
    return (left < right + second.get('column_span', 1)
            and right < left + first.get('column_span', 1))


def _merge_origins(shape, rows):
    """Return explicit restart cells needed by included continuation rows."""
    origins = []
    ordered = sorted(shape.get('rows', []), key=lambda row: row['row'])
    for row in ordered:
        if row['row'] not in rows:
            continue
        for cell in row.get('cells', []):
            if cell.get('vertical_merge') != 'continue':
                continue
            for previous in reversed([item for item in ordered if item['row'] < row['row']]):
                overlap = [item for item in previous.get('cells', []) if _columns_overlap(item, cell)]
                restart = next((item for item in overlap if item.get('vertical_merge') == 'restart'), None)
                if restart:
                    origins.append((previous['row'], restart))
                    break
                # A normal cell ends the merge. An absent preceding row means
                # the supplied geometry is partial; do not invent an origin.
                if any(not item.get('vertical_merge') for item in overlap):
                    break
    return origins


def split_hydrated_batch(batch):
    """Safely bisect one hydrated batch, shrinking context and geometry too.

    Source intervals are neither dropped nor duplicated. Original header/key
    cells can move to a child's context; they are never counted as new source
    coverage. Necessary context that cannot fit is an explicit failure, rather
    than an endlessly recurring split with the same oversized input.
    """
    sources = [_hydrated_source(item) for item in batch.get('sources', [])]
    if not sources:
        raise ValueError('提取批次缺少原文，无法拆分')
    total = sum(len(item['text']) for item in sources)
    if total != batch.get('characters', total):
        raise ValueError('提取批次覆盖字数与实际原文不一致，无法安全拆分')
    if total < 2:
        raise ValueError('原文单元已无法继续缩小，请检查模型输出限制')
    ranges = defaultdict(list)
    for item in sources:
        ranges[(item['citation'].split('#', 1)[0], item['block'])].append((item['start'], item['end']))
    for intervals in ranges.values():
        ordered = sorted(intervals)
        if any(first[1] > second[0] for first, second in zip(ordered, ordered[1:])):
            raise ValueError('原批次的主原文区间存在重叠，无法安全拆分')
    pool = {}
    source_citations = {item['citation'] for item in sources}
    for item in sources + [_hydrated_source(item) for item in batch.get('context', [])]:
        previous = pool.get(item['citation'])
        pool[item['citation']] = {**(previous or {}), **item}
        if previous and previous.get('context_reason'):
            pool[item['citation']]['context_reason'] = previous['context_reason']
    for source in list(pool.values()):
        for heading in source.get('heading_path', []):
            if heading.get('text') and heading.get('citation') not in pool:
                pool[heading['citation']] = _hydrated_source(dict(heading, context_reason='所属章节原文'))
    geometries = batch.get('table_structure') or []
    if not isinstance(geometries, list) or any(not isinstance(item, dict) or not item.get('id') for item in geometries):
        raise ValueError('表格几何结构无效，无法安全拆分')
    shapes = {shape['id']: shape for shape in geometries}

    def child(group, ordinal):
        ids = list(dict.fromkeys(item['table_id'] for item in group if item.get('table_id')))
        rows = defaultdict(set)
        for item in group:
            if item.get('table_id'):
                rows[item['table_id']].add(item.get('table_row'))
        contexts = []
        seen = set()

        def add(item, reason):
            if not item or item['citation'] in seen:
                return
            # Subtract ranges already in sources/context. This matters when a
            # single long header/cell is recursively cut: keeping the original
            # whole-cell citation would undo every split and grow the budget.
            remaining = [(item['start'], item['end'])]
            for covered in group + contexts:
                if item['block'] != covered['block'] or item['citation'].split('#')[0] != covered['citation'].split('#')[0]:
                    continue
                next_ranges = []
                for start, end in remaining:
                    if covered['end'] <= start or covered['start'] >= end:
                        next_ranges.append((start, end))
                    else:
                        if start < covered['start']:
                            next_ranges.append((start, covered['start']))
                        if covered['end'] < end:
                            next_ranges.append((covered['end'], end))
                remaining = next_ranges
            for start, end in remaining:
                candidate = item if start == item['start'] and end == item['end'] else _sliced_source(item, start - item['start'], end - item['start'])
                contexts.append(dict(candidate, context_reason=reason))
                seen.add(candidate['citation'])

        for source in group:
            for heading in source.get('heading_path', []):
                add(pool.get(heading['citation']), '所属章节原文')
        for table in ids:
            available = [item for item in pool.values() if item.get('table_id') == table]
            shape = shapes.get(table, {})
            header_rows = {row['row'] for row in shape.get('rows', []) if row.get('header')}
            header_rows.update(item.get('table_row') for item in available
                               if item.get('table_header') or '表头' in item.get('context_reason', ''))
            if not header_rows and available:
                first = min(item.get('table_row', 1) for item in available)
                header_rows.add(first)
                if any(item.get('column_span', 1) > 1 or item.get('vertical_merge') == 'restart'
                       for item in available if item.get('table_row') == first):
                    header_rows.add(first + 1)
            for item in available:
                if item.get('table_row') in header_rows:
                    add(item, item.get('context_reason') or '表头候选原文')
            for row in rows[table]:
                leading = sorted([item for item in available if item.get('table_row') == row],
                                 key=lambda item: (item.get('column', item.get('table_cell', 1)), item.get('paragraph_offset', 0)))
                for item in leading[:2]:
                    if len(item['text']) > 1200:
                        item = _sliced_source(item, 0, 1200)
                    add(item, '同一表格行的行首单元格原文')
            for row_number, cell in _merge_origins(shape, rows[table]):
                for item in available:
                    if item.get('table_row') == row_number and _columns_overlap(item, cell):
                        add(item, '纵向合并单元格起点原文')
            for item in pool.values():
                reason = item.get('context_reason', '')
                if ('表前' in reason or '表后' in reason) and item.get('context_table_id', table) == table:
                    add(item, reason)
        # Table captions/notes may have been a source in the parent, and are
        # now on the other side of the split. Recover only real adjacent text.
        for table in ids:
            positions = [i for i, item in enumerate(sources) if item.get('table_id') == table]
            if positions:
                for item in sources[max(0, min(positions) - 3):min(positions)]:
                    if not item.get('table_id') and (len(item['text']) <= 200 or _CAPTION.match(item['text'])):
                        add(item, '表前相邻标题或单位原文')
                for item in sources[max(positions) + 1:max(positions) + 5]:
                    if item.get('table_id') or not _NOTE.match(item['text'].lstrip()):
                        break
                    add(item, '表后注释原文')
        # Preserve explicitly supplied cross references/definitions. Unknown
        # context is not safe to discard just because it is inconvenient.
        for item in pool.values():
            reason = item.get('context_reason', '')
            if item['citation'] in source_citations:
                continue
            if not any(term in reason for term in ('表头', '行首', '所属章节', '表前', '表后', '相邻', '纵向合并')):
                if not item.get('table_id') or item.get('table_id') in ids:
                    add(item, reason or '原批必要上下文')
        first = group[0]
        own_index = next((i for i, item in enumerate(sources) if item['citation'] == first['citation']), None)
        if own_index is not None and own_index > 0:
            previous = sources[own_index - 1]
            if not first.get('table_id') and not previous.get('table_id'):
                add(previous if len(previous['text']) <= 1200 else _sliced_source(previous, len(previous['text']) - 1200, len(previous['text'])), '拆分前相邻原文')
        elif ordinal == 1:
            for item in batch.get('context', []):
                if '上一批相邻' in item.get('context_reason', ''):
                    add(_hydrated_source(item), item['context_reason'])
        if own_index is None and not first.get('table_id'):
            containing = next((item for item in sources if item['block'] == first['block']
                               and item['start'] <= first['start'] < item['end']), None)
            if containing and containing['start'] < first['start']:
                end = first['start'] - containing['start']
                add(_sliced_source(containing, max(0, end - 1200), end), '拆分前相邻原文')
        last = group[-1]
        if not last.get('table_id'):
            containing = next((item for item in sources if item['block'] == last['block']
                               and item['start'] < last['end'] <= item['end']), None)
            if containing and last['end'] < containing['end']:
                start = last['end'] - containing['start']
                add(_sliced_source(containing, start, min(len(containing['text']), start + 1200)), '拆分后相邻原文')
        context_characters = sum(len(item['text']) for item in contexts)
        if context_characters > MAX_SPLIT_CONTEXT_CHARACTERS:
            raise ValueError('必要表头、行首或定义上下文超过8000字，无法安全缩小；需按表格列或章节重新规划')
        structure = []
        for table in ids:
            if table not in shapes:
                continue
            required_rows = set(rows[table])
            required_rows.update(item.get('table_row') for item in contexts if item.get('table_id') == table)
            required_rows.update(row for row, _ in _merge_origins(shapes[table], required_rows))
            structure.append({**shapes[table], 'rows': [row for row in shapes[table].get('rows', []) if row['row'] in required_rows]})
        if len(json.dumps(structure, ensure_ascii=False)) > MAX_SPLIT_GEOMETRY_CHARACTERS:
            raise ValueError('必要表格合并结构超过64000字符，无法安全缩小；需重新规划表格范围')
        signature = '|'.join(item['citation'] + ':' + item['sha256'] for item in group)
        return {**batch, 'id': 'batch-' + _digest(signature)[:24], 'sources': group, 'context': contexts,
                'characters': sum(len(item['text']) for item in group), 'context_characters': context_characters,
                'unit_ids': [item['unit_id'] for item in group], 'table_ids': ids, 'table_structure': structure}

    # Try balanced whole-source/whole-row cuts first. Both children must have
    # smaller primary coverage. Only then try cutting a long single source.
    choices = []
    cumulative = 0
    for position, item in enumerate(sources[:-1], 1):
        cumulative += len(item['text'])
        same_row = (item.get('table_id') and item.get('table_id') == sources[position].get('table_id')
                    and item.get('table_row') == sources[position].get('table_row'))
        choices.append((bool(same_row), abs(total / 2 - cumulative), position))
    errors = []
    for _, _, position in sorted(choices):
        try:
            return [child(sources[:position], 1), child(sources[position:], 2)]
        except ValueError as exc:
            errors.append(str(exc))
    if len(sources) == 1 and len(sources[0]['text']) >= 2:
        source = sources[0]
        middle = len(source['text']) // 2
        # Prefer a nearby sentence boundary only if both halves remain useful.
        boundaries = [source['text'].rfind(separator, max(1, middle - 250), middle + 250)
                      for separator in ('\n', '。', '；')]
        boundary = max(boundaries)
        if 0 <= boundary < len(source['text']) - 1:
            middle = boundary + 1
        left, right = _sliced_source(source, 0, middle), _sliced_source(source, middle, len(source['text']))
        try:
            return [child([left], 1), child([right], 2)]
        except ValueError as exc:
            errors.append(str(exc))
    raise ValueError(errors[0] if errors else '该原文单元没有保持必要上下文的安全拆分方式')


def build_plan(store, industry, material_id):
    """Return every indexed source range and bounded extraction batches.

    ``characters`` counts source text once, excluding repeated context and
    generated DOCX location markers. All offsets are zero-based half-open in
    memory and converted to the existing one-based inclusive citation syntax.
    ``table_structure`` preserves Word grid spans/vertical merge declarations;
    table header inference is labelled and never asserted as author-supplied.
    """
    knowledge = Knowledge(store)
    material = knowledge.get(industry, material_id)
    shapes = {}
    limitations = []
    if material.get('extraction', {}).get('text_index'):
        stored_parts = [part for _, part in knowledge.index_parts(material)]
        structured, shapes, notes = _docx_structure(knowledge, industry, material, stored_parts)
        limitations.extend(notes)
        units = []
        heading_path = []
        for block, part in enumerate(stored_parts, 1):
            metadata = structured[block - 1]['units'] if structured else _fallback_units(part)
            for entry in metadata:
                start, end = entry['start'], entry['end']
                if not 0 <= start < end <= len(part['text']):
                    raise ValueError('正文索引的原文区间无效')
                source = _span(material_id, block, part['text'], start, end, entry)
                level = entry.get('heading_level')
                if level:
                    heading_path = heading_path[:level - 1]
                    heading_path.append({'text': source['text'], 'citation': source['citation'],
                                         'location': source['location'], 'level': level})
                source['heading_path'] = [dict(heading) for heading in heading_path]
                units.append(source)
        expected = material.get('extraction', {}).get('characters')
        actual = sum(len(item['text']) for item in units)
        if expected is not None and actual != expected:
            raise ValueError(f'全文索引文字总数不一致（资料{expected}字，计划{actual}字），不能宣称全文覆盖')
    else:
        units = _paragraph_units(material)
        limitations.append('当前按资料中已保存的文字处理；非原生文字、附件中未解析内容不在本次文字范围。')
    if not units:
        raise ValueError('资料没有可提取的文字')
    contexts = _table_context(units)
    row_sources = defaultdict(list)
    for source in units:
        if source.get('table_id'):
            row_sources[(source['table_id'], source.get('table_row'))].append(source)
    groups = _groups(units)
    batches = []
    batch_sources = []
    count = 0

    def flush():
        nonlocal batch_sources, count
        if not batch_sources:
            return
        ids = list(dict.fromkeys(item['table_id'] for item in batch_sources if item.get('table_id')))
        context = []
        seen = {item['citation'] for item in batch_sources}
        for table in ids:
            for item in contexts[table]:
                if item['citation'] not in seen:
                    context.append(item)
                    seen.add(item['citation'])
        # If an exceptionally wide row has to span batches, retain its key
        # cells as context rather than separating later values from the row.
        for item in batch_sources:
            if item.get('table_id'):
                for leading in row_sources[(item['table_id'], item.get('table_row'))][:2]:
                    candidate = _split_source(leading, 1200)[0]
                    if candidate['citation'] not in seen:
                        context.append(dict(candidate, context_reason='同一表格行的行首单元格原文'))
                        seen.add(candidate['citation'])
        for table in ids:
            current_rows = {item.get('table_row') for item in batch_sources if item.get('table_id') == table}
            for row_number, cell in _merge_origins(shapes.get(table, {}), current_rows):
                for item in row_sources[(table, row_number)]:
                    if _columns_overlap(item, cell) and item['citation'] not in seen:
                        context.append(dict(item, context_reason='纵向合并单元格起点原文', context_table_id=table))
                        seen.add(item['citation'])
        # Headings are source citations too, never model-generated summaries.
        for item in batch_sources:
            for heading in item.get('heading_path', []):
                if heading['citation'] not in seen:
                    context.append(dict(heading, context_reason='所属章节原文'))
                    seen.add(heading['citation'])
        # A short preceding unit helps definitions that straddle a batch.
        if batches:
            previous = batches[-1]['sources'][-1]
            if previous['citation'] not in seen and len(previous['text']) <= 1200:
                context.append(dict(previous, context_reason='上一批相邻原文'))
        signature = '|'.join(item['citation'] + ':' + item['sha256'] for item in batch_sources)
        rows = defaultdict(set)
        for item in batch_sources:
            if item.get('table_id'):
                rows[item['table_id']].add(item.get('table_row'))
        structure = []
        for table in ids:
            if table in shapes:
                header_rows = {item.get('table_row') for item in context + contexts[table] if item.get('table_id') == table}
                shape = shapes[table]
                structure.append({**shape, 'rows': [row for row in shape['rows'] if row['row'] in rows[table] | header_rows]})
        batches.append({'id': 'batch-' + _digest(signature)[:24], 'sources': batch_sources,
                        'context': context, 'unit_ids': [item['unit_id'] for item in batch_sources],
                        'characters': count, 'context_characters': sum(len(item['text']) for item in context),
                        'table_ids': ids, 'table_structure': structure})
        batch_sources, count = [], 0

    for group in groups:
        size = sum(len(item['text']) for item in group)
        # Keep a natural heading with the following paragraph where possible.
        new_heading = group[0].get('heading_level')
        if batch_sources and (count + size > MAX_CHARACTERS or count >= TARGET_CHARACTERS or (new_heading and count >= 4000)):
            flush()
        batch_sources.extend(group)
        count += size
    flush()
    unique_tables = {item['table_id'] for item in units if item.get('table_id')}
    table_count = max(len(shapes), len(unique_tables), int(material.get('extraction', {}).get('tables') or 0))
    image_count = int(material.get('extraction', {}).get('images') or 0)
    if image_count:
        limitations.append(f'另有{image_count}个媒体文件；图片未做OCR或视觉识别，数量不等于图片出现次数。')
    if table_count:
        limitations.append('表格按Word XML行列与合并声明提供结构；未标记表头使用明确标注的首行候选，不保证视觉版式与渲染页码。')
    all_sources = [source for batch in batches for source in batch['sources']]
    return {'material': material, 'fingerprint': kb.material_fingerprint(material), 'batches': batches,
            'total_characters': sum(batch['characters'] for batch in batches), 'total_units': len(all_sources),
            'table_count': table_count, 'image_count': image_count,
            'limitations': limitations, 'parser_version': PARSER_VERSION,
            'coverage_scope': 'indexed_text_and_native_tables',
            'table_structure_available': bool(shapes),
            'tables_without_text': [table for table in shapes if table not in unique_tables],
            'source_manifest_hash': _digest('|'.join(item['citation'] + ':' + item['sha256'] for item in all_sources))}
