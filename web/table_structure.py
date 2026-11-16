"""Bounded table structure and semantic row context, without model-generated values."""
import csv
from decimal import Decimal, InvalidOperation
import io
import re

MAX_CELLS=100_000


def unit_hint(text):
    explicit=re.search(r'单位\s*[:：]\s*([^\n，,；;。]{1,24})',text)
    if explicit:return explicit[1].strip().rstrip('）)')
    parenthesis=re.search(r'[（(]([^()（）]{1,20})[)）]',text)
    if parenthesis and not re.fullmatch(r'\d{2,4}年(?:\d{1,2}月)?',parenthesis[1]) and re.search(r'元|%|％|吨|米|亩|人|个|件|台|千克|公斤|秒|小时|年|月|℃|度|倍|kg|kW|MW|USD|CNY|m²|m³',parenthesis[1],re.I):return parenthesis[1]
    return ''


def table_from_cells(cells,caption='',notes=None,header_rows=None,source=None,table_id='table-1'):
    if not cells:raise ValueError('表格没有单元格')
    if len(cells)>MAX_CELLS:raise ValueError('单表超过10万个单元格，请拆分')
    if any(type(c.get(k,1)) is not int or c.get(k,1)<1 for c in cells for k in ('row','column','row_span','column_span')):
        raise ValueError('单元格行列和合并范围必须为正整数')
    rows=max(c['row']+c.get('row_span',1)-1 for c in cells)
    columns=max(c['column']+c.get('column_span',1)-1 for c in cells)
    if rows*columns>MAX_CELLS or columns>300:raise ValueError('表格行列规模超过处理限制')
    grid=[[None]*columns for _ in range(rows)]
    for index,cell in enumerate(cells,1):
        cell.setdefault('id',f'{table_id}-r{cell["row"]}-c{cell["column"]}')
        cell.setdefault('row_span',1);cell.setdefault('column_span',1)
        cell.setdefault('text','')
        for row in range(cell['row']-1,cell['row']+cell['row_span']-1):
            for column in range(cell['column']-1,cell['column']+cell['column_span']-1):
                if grid[row][column] is not None and grid[row][column]!=cell['id']:
                    raise ValueError('表格合并单元格范围重叠')
                grid[row][column]=cell['id']
    lookup={c['id']:c for c in cells}
    detected=header_rows is None
    if header_rows is None:
        declared=[c['row'] for c in cells if c.get('header')]
        header_rows=max(declared,default=1)
        if not declared and rows>1 and any(c['row']==1 and c['column_span']>1 for c in cells):header_rows=2
        if not declared and rows>1 and any(c['row']==1 and re.match(r'\s*单位\s*[:：]',c['text']) for c in cells):header_rows=2
    if not 0<=header_rows<=min(rows,10):raise ValueError('表头行数无效')
    notes=list(notes or []);note_rows=[];note_cell_ids=[]
    for row in range(header_rows+1,rows+1):
        row_cells=[c for c in cells if c['row']==row]
        if row_cells and re.match(r'\s*(?:注[：:]|注\d|备注[：:]?|说明[：:]|数据来源[：:])',row_cells[0]['text']):
            notes.append(' '.join(c['text'] for c in row_cells if c['text']));note_rows.append(row);note_cell_ids.extend(c['id'] for c in row_cells)
    global_unit=unit_hint(caption) or next((unit_hint(c['text']) for c in cells if c['row']<=header_rows and re.search(r'单位\s*[:：]',c['text'])),'')
    headers=[]
    for column in range(columns):
        path=[];ids=[]
        for row in range(header_rows):
            key=grid[row][column]
            if key and key not in ids:
                ids.append(key);text=lookup[key]['text']
                if text:path.append(text)
        headers.append({'column':column+1,'path':path,'cell_ids':ids,
            'label':' / '.join(path) or f'未标注列{column+1}',
            'unit':unit_hint(' '.join(path)) or ('' if path and re.fullmatch(r'地区|区域|省份|国家|名称|项目|指标|类别|时间|年份|年度|月份|日期|序号|编号|(?:计量)?单位',path[-1]) else global_unit)})
    return {'id':table_id,'caption':caption or table_id,'notes':list(dict.fromkeys(notes)),'note_rows':note_rows,'note_cell_ids':note_cell_ids,'source':source or {},
        'rows':rows,'columns':columns,'header_rows':header_rows,
        'header_detection':'heuristic' if detected and not any(c.get('header') for c in cells) else 'native_or_user',
        'cells':cells,'grid':grid,'headers':headers,'quality':'parsed_not_verified'}


def from_docx(blocks,stats,header_rows=None):
    text={};locations={};preceding={};following={};last=[];last_table=None
    for block in blocks:
        if block.get('kind')=='table_cell':
            key=(block['table_id'],block['table_row'],block['table_cell'])
            text.setdefault(key,[]).append(block['text']);locations.setdefault(key,[]).append(block['location'])
            preceding.setdefault(block['table_id'],last[-3:]);last_table=block['table_id']
        else:
            if last_table and re.match(r'\s*(?:注[：:]|注\d|备注|说明[：:]|数据来源)',block['text']):
                following.setdefault(last_table,[]).append(block['text'])
            last.append(block['text'])
            if len(last)>3:last.pop(0)
    tables=[]
    for shape in stats.get('table_structure',[]):
        cells=[];active={}
        for row in shape['rows']:
            for raw in row['cells']:
                column=raw['column'];span=raw['column_span']
                if raw['vertical_merge']=='continue' and column in active:
                    origin=active[column]
                    origin['row_span']=max(origin['row_span'],row['row']-origin['row']+1)
                    continue
                key=(shape['id'],row['row'],raw['cell'])
                cell={'row':row['row'],'column':column,'column_span':span,'row_span':1,
                    'text':'\n'.join(text.get(key,[])),'header':row['header'],
                    'source':{'part':shape['part'],'table':shape['id'],'row':row['row'],'cell':raw['cell'],'locations':locations.get(key,[])}}
                cells.append(cell)
                for c in range(column,column+span):
                    if raw['vertical_merge']=='restart':active[c]=cell
                    else:active.pop(c,None)
        caption=next((s for s in reversed(preceding.get(shape['id'],[])) if re.match(r'\s*表\s*[\d一二三四五六七八九十]',s)),'')
        if not caption:caption='表'+str(len(tables)+1)
        units=[s for s in preceding.get(shape['id'],[]) if re.search(r'单位\s*[:：]',s)]
        if units:caption+=' · '+units[-1]
        if cells:tables.append(table_from_cells(cells,caption,following.get(shape['id']),header_rows,
            {'format':'docx','part':shape['part'],'native_table':shape['id'],'depth':shape['depth']},'table-'+str(len(tables)+1)))
    return tables


def from_csv(text,caption='',header_rows=None):
    try:dialect=csv.Sniffer().sniff(text[:8192],delimiters=',;\t')
    except csv.Error:dialect=csv.excel
    reader=csv.reader(io.StringIO(text),dialect);cells=[];previous_line=0
    for row,values in enumerate(reader,1):
        if row>MAX_CELLS:raise ValueError('CSV行数超出限制')
        for column,value in enumerate(values,1):
            cells.append({'row':row,'column':column,'text':value,
                'source':{'format':'csv','line_start':previous_line+1,'line_end':reader.line_num,'column':column}})
            if len(cells)>MAX_CELLS:raise ValueError('CSV超过10万个单元格')
        previous_line=reader.line_num
    return [table_from_cells(cells,caption,header_rows=header_rows,source={'format':'csv'})] if cells else []


def from_html(markup,caption='',header_rows=None):
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(markup,'html.parser');tables=[]
    for number,node in enumerate(soup.find_all('table'),1):
        cells=[];occupied=set()
        for row,tr in enumerate(node.find_all('tr',recursive=True),1):
            if tr.find_parent('table') is not node:continue
            column=1
            for td in tr.find_all(['td','th'],recursive=False):
                while (row,column) in occupied:column+=1
                try:rs,cs=int(td.get('rowspan',1)),int(td.get('colspan',1))
                except (TypeError,ValueError):raise ValueError('HTML表格合并范围无效')
                if not 1<=rs<=1000 or not 1<=cs<=300:raise ValueError('HTML表格合并范围过大')
                cells.append({'row':row,'column':column,'row_span':rs,'column_span':cs,
                    'text':td.get_text(' ',strip=True),'header':td.name=='th' and (tr.find_parent('thead') is not None or all(x.name=='th' for x in tr.find_all(['td','th'],recursive=False))),
                    'source':{'format':'html','table':number,'row':row,'column':column}})
                occupied.update((r,c) for r in range(row,row+rs) for c in range(column,column+cs))
                column+=cs
        if cells:
            cap=node.find('caption',recursive=False)
            tables.append(table_from_cells(cells,cap.get_text(' ',strip=True) if cap else caption or f'表{number}',
                header_rows=header_rows,source={'format':'html','table':number},table_id=f'table-{number}'))
    return tables


def row_records(table):
    """Derived row descriptions retain headers/units/notes and exact input cell IDs."""
    lookup={c['id']:c for c in table['cells']}
    for number,row in enumerate(table['grid'][table['header_rows']:],table['header_rows']+1):
        if number in table.get('note_rows',[]):continue
        values=[];seen=set();references=[]
        for column,key in enumerate(row):
            if not key or key in seen:continue
            seen.add(key);cell=lookup[key];header=table['headers'][column]
            missing_formula=cell.get('source',{}).get('formula') is not None and not cell.get('source',{}).get('cached_value_available')
            value=cell['text'] if cell['text'] else '公式结果未缓存（未执行计算，不能当成0）' if missing_formula else '空白（不等于0）'
            values.append(header['label']+'：'+value+
                (f'〔单位提示：{header["unit"]}〕' if header['unit'] else ''))
            references.extend(header['cell_ids']+[key])
        if not values:continue
        text='结构化表格行（'+('机器识别重建；' if table.get('quality')!='parsed_not_verified' else '')+'按单元格组织，非原文连续段落；未核验）\n表名：'+table['caption']+'\n'+'；'.join(values)
        if table['notes']:text+='\n表注：'+'；'.join(table['notes'])
        yield {'text':text,'kind':'structured_table_row','location':table['caption']+f'·行{number}',
            'table_id':table['id'],'row':number,'cell_ids':list(dict.fromkeys(references)),
            'source':table['source'],'quality':table.get('quality','parsed_not_verified')}


def markdown(table,max_rows=100):
    lookup={c['id']:c for c in table['cells']}
    clean=lambda value:str(value).replace('|','\\|').replace('\n','<br>')
    lines=['| '+' | '.join(clean(h['label']) for h in table['headers'])+' |',
           '| '+' | '.join('---' for _ in table['headers'])+' |']
    for row in table['grid'][table['header_rows']:table['header_rows']+max_rows]:
        lines.append('| '+' | '.join(clean(lookup[key]['text'] or ('公式结果未缓存' if lookup[key].get('source',{}).get('formula') is not None and not lookup[key].get('source',{}).get('cached_value_available') else '')) if key else '' for key in row)+' |')
    return '\n'.join(lines)


def calculate(table,column,rows,operation):
    """Explicit row selection avoids silently adding totals to detail rows."""
    if operation not in ('sum','mean','min','max') or not isinstance(rows,list) or not rows or any(type(r) is not int for r in rows) or len(set(rows))!=len(rows):
        raise ValueError('请选择明确且不重复的数据行及计算方式')
    if type(column) is not int or not 1<=column<=table['columns']:raise ValueError('列号无效')
    lookup={c['id']:c for c in table['cells']};values=[];cells=[];units=[]
    unit_column=next((h['column'] for h in table['headers'] if re.fullmatch(r'(?:计量)?单位',h['label'])),None)
    for row in rows:
        if not table['header_rows']<row<=table['rows'] or row in table.get('note_rows',[]):raise ValueError('计算只能选择表头之后的数据行，不能包含表注')
        key=table['grid'][row-1][column-1]
        if not key or key in cells:raise ValueError('空白或跨行合并单元格不能重复计数')
        raw=lookup[key]['text'].strip()
        if not re.fullmatch(r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?',raw):
            raise ValueError('所选单元格并非同一列的纯数值；缺失、单位和百分比需先明确口径')
        values.append(Decimal(raw.replace(',','')));cells.append(key)
        unit=table['headers'][column-1]['unit']
        if unit_column:
            unit_key=table['grid'][row-1][unit_column-1]
            unit=lookup[unit_key]['text'].strip() if unit_key else ''
            if not unit:raise ValueError('所选数据行的单位未披露，不能假定同一单位')
            cells.append(unit_key)
        units.append(unit)
    if len(set(units))!=1:raise ValueError('所选数据行的单位不同，未自动换算或相加')
    result=sum(values) if operation=='sum' else sum(values)/len(values) if operation=='mean' else min(values) if operation=='min' else max(values)
    return {'operation':operation,'value':str(result),'unit':units[0],
        'column':column,'rows':rows,'input_cell_ids':list(dict.fromkeys(cells)),
        'header_cell_ids':table['headers'][column-1]['cell_ids'],'nature':'calculated_from_selected_cells',
        'note':'按选定单元格计算，不是原文逐字记载或外部核实；请确认所选行的指标、期间与统计口径适合本次计算。'}
