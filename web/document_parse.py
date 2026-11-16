"""Native tables and bounded image extraction from local attachments."""
import io
import hashlib
import json
from pathlib import Path
import re
import zipfile

from .docx_index import extract_docx
from .table_structure import MAX_CELLS,from_docx,from_csv,from_html,table_from_cells


def image_key(source):return 'figure-'+hashlib.sha256(json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:12]


def png_bytes(raw):
    from PIL import Image,ImageOps
    Image.MAX_IMAGE_PIXELS=20_000_000
    with Image.open(io.BytesIO(raw)) as original:
        if original.width*original.height>20_000_000:raise ValueError('图片超过2000万像素')
        image=ImageOps.exif_transpose(original).convert('RGB')
        image.thumbnail((2200,2200))
        output=io.BytesIO();image.save(output,format='PNG')
        return output.getvalue(),image.size


def safe_archive(path):
    archive=zipfile.ZipFile(path)
    infos=archive.infolist()
    if len(infos)>15000 or len({i.filename for i in infos})!=len(infos) or sum(i.file_size for i in infos)>500_000_000:
        archive.close();raise ValueError('文档归档条目或解压总量超过限制')
    if any(i.flag_bits&1 for i in infos):archive.close();raise ValueError('不处理加密文档')
    return archive


def xlsx_tables(path,header_rows=None):
    import openpyxl
    with safe_archive(path) as archive:
        if any(i.file_size>64_000_000 for i in archive.infolist() if i.filename.endswith('.xml')):raise ValueError('单份表格XML超过64MB')
    formulas=openpyxl.load_workbook(path,data_only=False,read_only=False,keep_links=False)
    values=openpyxl.load_workbook(path,data_only=True,read_only=False,keep_links=False)
    tables=[];count=0
    try:
        for sheet in formulas:
            if sheet.max_row*sheet.max_column>MAX_CELLS:raise ValueError('工作表超过10万个单元格')
            merged={};covered=set()
            for area in sheet.merged_cells.ranges:
                merged[(area.min_row,area.min_col)]=(area.max_row-area.min_row+1,area.max_col-area.min_col+1)
                covered.update((r,c) for r in range(area.min_row,area.max_row+1) for c in range(area.min_col,area.max_col+1) if (r,c)!=(area.min_row,area.min_col))
            cells=[]
            for row in sheet:
                for cell in row:
                    if (cell.row,cell.column) in covered:continue
                    count+=1
                    if count>MAX_CELLS:raise ValueError('工作簿超过10万个单元格')
                    cached=values[sheet.title].cell(cell.row,cell.column).value
                    value=cached if cell.data_type=='f' else cell.value
                    text='' if value is None else value.isoformat() if hasattr(value,'isoformat') else str(value)
                    if isinstance(value,(int,float)) and '%' in cell.number_format:
                        text=format(value*100,'.12g')+'%'
                    rs,cs=merged.get((cell.row,cell.column),(1,1))
                    cells.append({'row':cell.row,'column':cell.column,'row_span':rs,'column_span':cs,'text':text,
                        'source':{'format':'xlsx','sheet':sheet.title,'coordinate':cell.coordinate,
                            'formula':cell.value if cell.data_type=='f' else None,'cached_value_available':value is not None,
                            'number_format':cell.number_format}})
            if cells:tables.append(table_from_cells(cells,sheet.title,header_rows=header_rows,
                source={'format':'xlsx','sheet':sheet.title,'formula_policy':'Cached values only; no formulas, macros or external links executed'},table_id=f'table-{len(tables)+1}'))
    finally:formulas.close();values.close()
    return tables


def parse_native(record,path,options):
    suffix=Path(path).suffix.lower() if path else ''
    result={'tables':[],'images':[],'notices':[],'coverage':{'pages_total':None,'pages_processed':None,'images_total':0,'images_selected':0,'native_tables':0,'image_sources':[],'pages_covered':[]}}
    header_rows=options.get('header_rows')
    image_limit=options['max_images'];page_limit=options['max_pages']
    skip=set(options.get('skip_figures',[]))
    def wanted(source):
        key=image_key(source)
        return key not in skip and len(result['images'])<image_limit
    def add_image(raw,source,caption=''):
        result['coverage']['image_sources'].append(source)
        if not wanted(source):return
        try:
            png,size=png_bytes(raw)
            result['images'].append({'png':png,'size':size,'source':source,'caption':caption})
        except Exception as exc:result['notices'].append('图像未解码：'+type(exc).__name__)
    if suffix=='.docx':
        blocks,stats=extract_docx(path,include_structure=True)
        result['tables']=from_docx(blocks,stats,header_rows)
        with safe_archive(path) as archive:
            images=[i for i in archive.infolist() if i.filename.startswith('word/media/')]
            result['coverage']['images_total']=len(images)
            for item in images:
                if len(result['images'])>=image_limit:
                    result['coverage']['image_sources'].append({'format':'docx','part':item.filename});continue
                if item.file_size>20_000_000:
                    result['coverage']['image_sources'].append({'format':'docx','part':item.filename})
                    result['notices'].append('嵌入媒体超过20MB，未解码');continue
                add_image(archive.read(item),{'format':'docx','part':item.filename},'文档嵌入图像 '+Path(item.filename).name)
    elif suffix=='.xlsx':
        result['tables']=xlsx_tables(path,header_rows)
    elif suffix=='.pdf':
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages)>300:raise ValueError('PDF超过300页')
            first=options.get('start_page',1)-1
            chosen=pdf.pages[first:first+page_limit]
            result['coverage'].update(pages_total=len(pdf.pages),pages_processed=len(chosen),pages_covered=[p.page_number for p in chosen])
            for page in chosen:
                text=page.extract_text() or ''
                for number,table in enumerate(page.find_tables(),1):
                    values=table.extract();cells=[]
                    for row,line in enumerate(values,1):
                        for column,value in enumerate(line,1):
                            if value is None:continue
                            # Geometric spans remain in the source metadata; do not
                            # invent repeated values for unpopulated PDF cells.
                            box=table.rows[row-1].cells[column-1] if column<=len(table.rows[row-1].cells) else None
                            cells.append({'row':row,'column':column,'text':value,
                                'source':{'format':'pdf','page':page.page_number,'bbox':list(box) if box else None}})
                    if cells:result['tables'].append(table_from_cells(cells,f'第{page.page_number}页表{number}',header_rows=header_rows,
                        source={'format':'pdf','page':page.page_number,'bbox':list(table.bbox)},table_id=f'table-{len(result["tables"])+1}'))
                visual=bool(page.images or page.curves or len(text.strip())<30)
                if visual:
                    result['coverage']['images_total']+=1
                    source={'format':'pdf','page':page.page_number,'bbox':list(page.bbox)}
                    if wanted(source):
                        image=page.to_image(resolution=140).original
                        output=io.BytesIO();image.save(output,format='PNG')
                        add_image(output.getvalue(),source,f'第{page.page_number}页图像预览')
                    else:result['coverage']['image_sources'].append(source)
            if len(chosen)<len(pdf.pages):result['notices'].append(f'本轮处理第{first+1}至{first+len(chosen)}页；图表完整覆盖见累计页清单')
    elif suffix in ('.png','.jpg','.jpeg','.tif','.tiff'):
        result['coverage']['images_total']=1
        add_image(Path(path).read_bytes(),{'format':'image','filename':record.get('attachment',{}).get('name','')},record['title'])
    else:
        if path:
            raw=Path(path).read_bytes()
            try:text=raw.decode('utf-8-sig')
            except UnicodeDecodeError:text=raw.decode('gb18030')
        else:text=record.get('content','')
        if suffix=='.csv':result['tables']=from_csv(text,record['title'],header_rows)
        elif suffix in ('.html','.htm') or '<table' in text.lower():result['tables']=from_html(text,record['title'],header_rows)
        elif suffix=='.md':
            lines=text.splitlines();groups=[];group=[]
            for line in lines+['']:
                if '|' in line and line.strip():group.append(line)
                elif group:
                    if len(group)>1:groups.append(group)
                    group=[]
            for group in groups:
                cells=[];row=0
                for line in group:
                    if re.fullmatch(r'[\s|:\-]+',line):continue
                    row+=1
                    for column,value in enumerate(line.strip().strip('|').split('|'),1):cells.append({'row':row,'column':column,'text':value.strip(),'source':{'format':'markdown','row':row,'column':column}})
                if cells:result['tables'].append(table_from_cells(cells,record['title'],header_rows=header_rows,source={'format':'markdown'},table_id=f'table-{len(result["tables"])+1}'))
    result['coverage'].update(images_selected=len(result['images']),native_tables=len(result['tables']))
    return result
