"""Bounded DOCX text extraction. No Office execution, media extraction or network."""
import json
import re
from pathlib import Path
import zipfile
from xml.etree import ElementTree as ET

W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MAX_FILE_BYTES=100_000_000
MAX_XML_BYTES=64_000_000
MAX_TEXT=2_000_000


class SafeXML:
    def __init__(self,stream):
        self.stream=stream
        self.tail=b''

    def read(self,size=-1):
        data=self.stream.read(size)
        probe=(self.tail+data).replace(b'\x00',b'').upper()
        if b'<!DOCTYPE' in probe or b'<!ENTITY' in probe:
            raise ValueError('不支持包含DTD或实体声明的文档XML')
        self.tail=probe[-16:]
        return data


def paragraph_text(element):
    parts=[]
    for node in element.iter():
        if node.tag==W+'t':
            parts.append(node.text or '')
        elif node.tag==W+'tab':
            parts.append('\t')
        elif node.tag in (W+'br',W+'cr'):
            parts.append('\n')
    return ''.join(parts).strip()


def extract_docx(source,include_structure=False):
    """Return ordered blocks, conservative locations and main-document statistics."""
    blocks=[]
    stats={'paragraphs':0,'tables':0,'images':0,'characters':0,'cached_page_count':None,'headings':[]}
    if include_structure:
        stats['table_structure']=[]
    with zipfile.ZipFile(source) as archive:
        infos=archive.infolist()
        names=[i.filename for i in infos]
        if len(infos)>15000 or len(names)!=len(set(names)) or sum(i.file_size for i in infos)>500_000_000:
            raise ValueError('DOCX包含重复条目、条目过多或解压总量超过500MB')
        if any(i.flag_bits & 1 for i in infos):
            raise ValueError('不处理加密DOCX')
        if 'word/document.xml' not in names:
            raise ValueError('DOCX缺少正文XML')
        stats['images']=sum(n.startswith('word/media/') for n in names)
        if 'docProps/app.xml' in names:
            info=archive.getinfo('docProps/app.xml')
            if info.file_size<=1_000_000:
                with archive.open(info) as stream:
                    props=ET.parse(SafeXML(stream)).getroot()
                stats['cached_page_count']=next((n.text for n in props if n.tag.endswith('}Pages')),None)
        heading_styles={}
        if 'word/styles.xml' in names and archive.getinfo('word/styles.xml').file_size<=4_000_000:
            with archive.open('word/styles.xml') as stream:
                styles=ET.parse(SafeXML(stream)).getroot()
            for style in styles.findall(W+'style'):
                name=style.find(W+'name')
                value=(name.get(W+'val','') if name is not None else '').casefold()
                level=style.find('./'+W+'pPr/'+W+'outlineLvl')
                named=re.fullmatch(r'(?:heading|标题)\s*([1-9])',value)
                if named or (level is not None and level.get(W+'val','').isdigit() and int(level.get(W+'val'))<9):
                    heading_styles[style.get(W+'styleId')]=int(named.group(1)) if named else int(level.get(W+'val'))+1
        parts=[('word/document.xml','正文')]
        parts += [(n, '脚注' if 'footnotes' in n else '尾注' if 'endnotes' in n else '页眉页脚 '+Path(n).stem)
                  for n in names if re.fullmatch(r'word/(?:footnotes|endnotes|header\d+|footer\d+)\.xml',n)]
        for filename,label in parts:
            info=archive.getinfo(filename)
            if info.file_size>(MAX_XML_BYTES if filename=='word/document.xml' else 4_000_000):
                raise ValueError('DOCX正文XML超过64MB或附属文字XML超过4MB')
            paragraph=0
            table=0
            stack=[]
            with archive.open(info) as stream:
                for event,element in ET.iterparse(SafeXML(stream),events=('start','end')):
                    if event=='start':
                        if element.tag==W+'tbl':
                            table+=1
                            stack.append({'id':table,'row':0,'cell':0,'column':0,'next_column':1,
                                          'header':False,'column_span':1,'vertical_merge':None})
                            if include_structure:
                                shape={'id':f'{filename}:table-{table}','part':filename,'grid':[],
                                       'depth':len(stack),'rows':[]}
                                stack[-1]['structure']=shape
                                stats['table_structure'].append(shape)
                        elif element.tag==W+'tr' and stack:
                            stack[-1]['row']+=1
                            stack[-1]['cell']=0
                            stack[-1]['next_column']=1
                            stack[-1]['header']=False
                            if include_structure:
                                stack[-1]['structure']['rows'].append({'row':stack[-1]['row'],'cells':[],'header':False})
                        elif element.tag==W+'tc' and stack:
                            stack[-1]['cell']+=1
                            stack[-1]['column']=stack[-1]['next_column']
                            stack[-1]['column_span']=1
                            stack[-1]['vertical_merge']=None
                        continue
                    if element.tag==W+'gridCol' and stack and include_structure:
                        stack[-1]['structure']['grid'].append(element.get(W+'w'))
                    elif element.tag==W+'trPr' and stack:
                        stack[-1]['header']=element.find(W+'tblHeader') is not None
                        if include_structure:
                            stack[-1]['structure']['rows'][-1]['header']=stack[-1]['header']
                        before=element.find(W+'gridBefore')
                        if before is not None and before.get(W+'val','').isdigit():
                            stack[-1]['next_column']=1+int(before.get(W+'val'))
                    elif element.tag==W+'tcPr' and stack:
                        span=element.find(W+'gridSpan')
                        if span is not None and span.get(W+'val','').isdigit():
                            stack[-1]['column_span']=max(1,int(span.get(W+'val')))
                        merge=element.find(W+'vMerge')
                        stack[-1]['vertical_merge']=(merge.get(W+'val') or 'continue') if merge is not None else None
                    elif element.tag==W+'p':
                        paragraph+=1
                        text=paragraph_text(element)
                        if text:
                            stats['characters']+=len(text)
                            if stats['characters']>MAX_TEXT:
                                raise ValueError('DOCX文字超过200万字，请按章节导入')
                            where=f'{label}·段落{paragraph}'
                            if stack:
                                t=stack[-1]
                                where=f"{label}·表{t['id']}·行{t['row']}·单元格{t['cell']}·段落{paragraph}"
                            style=element.find('./'+W+'pPr/'+W+'pStyle')
                            outline=element.find('./'+W+'pPr/'+W+'outlineLvl')
                            level=heading_styles.get(style.get(W+'val')) if style is not None else None
                            if outline is not None and outline.get(W+'val','').isdigit() and int(outline.get(W+'val'))<9:
                                level=int(outline.get(W+'val'))+1
                            if filename=='word/document.xml' and level and len(stats['headings'])<100:
                                stats['headings'].append({'text':text[:150],'location':where})
                            block={'text':text,'location':where,'part':filename,'kind':'paragraph','heading_level':level}
                            if stack:
                                block.update(kind='table_cell',table_id=f'{filename}:table-{t["id"]}',
                                    table_row=t['row'],table_cell=t['cell'],column=t['column'],
                                    column_span=t['column_span'],vertical_merge=t['vertical_merge'],
                                    table_header=t['header'],table_depth=len(stack))
                            blocks.append(block)
                        element.clear()
                    elif element.tag==W+'tbl':
                        stack.pop()
                        element.clear()
                    elif element.tag in (W+'tc',W+'tr'):
                        if element.tag==W+'tc' and stack:
                            if include_structure:
                                t=stack[-1]
                                t['structure']['rows'][-1]['cells'].append({
                                    'cell':t['cell'],'column':t['column'],'column_span':t['column_span'],
                                    'vertical_merge':t['vertical_merge']})
                            stack[-1]['next_column']=stack[-1]['column']+stack[-1]['column_span']
                        element.clear()
            if filename=='word/document.xml':
                stats['paragraphs']=paragraph
                stats['tables']=table
    return blocks,stats


def index_records(blocks):
    """Yield the unchanged v1 text plus exact content spans and optional structure."""
    parts=[]
    current=[]
    count=0
    first=None
    last=None
    def flush():
        nonlocal current,count,first,last
        if not current:
            return
        text='\n\n'.join(item['piece'] for item in current)
        cursor=0
        units=[]
        for item in current:
            prefix=len(item['piece'])-len(item['text'])
            units.append({k:v for k,v in item.items() if k not in ('piece','text')} |
                         {'start':cursor+prefix,'end':cursor+len(item['piece'])})
            cursor+=len(item['piece'])+2
        parts.append({'text':text,'first_location':first,'last_location':last,'units':units})
        current=[]
        count=0
        first=None
    for block in blocks:
        # A very long single paragraph is split without losing its paragraph locator.
        text=block['text']
        for offset in range(0,len(text),5000):
            piece=f"【{block['location']}】\n"+text[offset:offset+5000]
            if current and count+len(piece)>6000:
                flush()
            first=first or block['location']
            last=block['location']
            current.append({**{k:v for k,v in block.items() if k!='text'},
                            'piece':piece,'text':text[offset:offset+5000],'paragraph_offset':offset})
            count+=len(piece)+2
    flush()
    if not parts:
        raise ValueError('没有可检索的DOCX文字；图像未做OCR')
    return parts


def write_index(folder,blocks,stats):
    """Small immutable chunks; markers identify real XML paragraphs/table cells, not rendered pages."""
    folder.mkdir(parents=True,exist_ok=True)
    parts=[]
    for number,record in enumerate(index_records(blocks),1):
        name=f'{number:05d}.json'
        (folder/name).write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
        parts.append({'file':name,'first_location':record['first_location'],
                      'last_location':record['last_location'],'characters':len(record['text'])})
    result={**stats,'format':'docx-text-v1','structure_version':'docx-structure-v2','parts':parts}
    (folder/'index.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    return result
