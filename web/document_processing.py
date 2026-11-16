"""Explicit, persistent local chart/table processing with versioned source parts."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import threading

from .service import kb,ws
from .knowledge import Knowledge,redact
from .docx_index import index_records
from .document_parse import parse_native,image_key
from .document_ocr import recognize,ruled_tables
from .document_vision import LocalVision,close_vision
from .table_structure import row_records,table_from_cells,calculate

VERSION='document-structure-v1'


def base_record(record):
    result=deepcopy(record)
    extraction=result.get('extraction',{})
    if 'native_text_index' in extraction:
        native=extraction.get('native_text_index')
        if native:extraction['text_index']=native
        else:extraction.pop('text_index',None)
    return result


def input_fingerprint(record):return kb.material_fingerprint(base_record(record))


def figure_key(source):return image_key(source)


def no_credentials(value):
    text=kb.json_text(value)
    if redact(text)!=text:raise ValueError('解析文字含疑似访问凭据，已停止；请先从资料中移除凭据')


def load_analysis(store,record):
    pointer=record.get('extraction',{}).get('document_analysis')
    if not pointer:return None
    base=(store.home(record['industry_id'])/'materials/text').resolve()
    path=(store.home(record['industry_id'])/'materials'/pointer).resolve()
    if not path.is_relative_to(base) or path.name!='result.json':raise ValueError('图表解析路径无效')
    result=kb.read_json(path)
    if result.get('input_fingerprint')!=input_fingerprint(record):
        return {'status':'invalidated','note':'资料版本或适用信息已变化，请重新处理图表。','tables':[],'figures':[]}
    return result


def stable_table(table):
    key='table-'+hashlib.sha256(kb.json_text({'source':table['source'],'caption':table['caption']}).encode()).hexdigest()[:14]
    mapping={c['id']:f'{key}-r{c["row"]}-c{c["column"]}' for c in table['cells']}
    table['id']=key
    for cell in table['cells']:cell['id']=mapping[cell['id']]
    table['grid']=[[mapping.get(value,value) for value in row] for row in table['grid']]
    for header in table['headers']:header['cell_ids']=[mapping.get(value,value) for value in header['cell_ids']]
    table['note_cell_ids']=[mapping.get(value,value) for value in table.get('note_cell_ids',[])]
    return table


class DocumentPaused(ValueError):pass


class DocumentProcessing:
    def __init__(self,store):
        self.store=store;self.knowledge=Knowledge(store);self.jobs={};self.tasks={};self.stops={}
        self.folder=store.root/'data/document-jobs'

    def save_job(self,job):
        job['updated_at']=kb.now()
        kb.atomic_write(self.folder/(job['id']+'.json'),kb.json_text(job))

    def restore(self):
        for path in self.folder.glob('*.json'):
            job=kb.read_json(path)
            if job['status'] in ('queued','running'):
                job.update(status='interrupted',note='服务已重启，已保存步骤保留，可继续处理。')
                self.save_job(job)
            self.jobs[job['id']]=job

    def get(self,key):
        ws.safe_id(key)
        if key not in self.jobs:raise ValueError('图表处理任务不存在')
        self.store.entry(self.jobs[key]['industry_id'])
        return self.jobs[key]

    def list(self,industry='all'):
        allowed={entry['id'] for entry in self.store.research_entries(industry)}
        return [job for job in sorted(self.jobs.values(),key=lambda j:j['created_at'],reverse=True) if job['industry_id'] in allowed]

    def options(self,payload):
        result={'ocr':payload.get('ocr',True),'vision':payload.get('vision',True),
            'max_pages':payload.get('max_pages',12),'max_images':payload.get('max_images',4),'header_rows':payload.get('header_rows'),
            'start_page':payload.get('start_page',1)}
        if type(result['ocr']) is not bool or type(result['vision']) is not bool:raise ValueError('图像处理开关需为布尔值')
        if type(result['max_pages']) is not int or not 1<=result['max_pages']<=50:raise ValueError('每轮页数需为1至50')
        if type(result['max_images']) is not int or not 0<=result['max_images']<=8:raise ValueError('每轮图像数需为0至8')
        if type(result['start_page']) is not int or not 1<=result['start_page']<=300:raise ValueError('起始页需为1至300')
        if result['header_rows'] is not None and (type(result['header_rows']) is not int or not 0<=result['header_rows']<=10):raise ValueError('表头行数需为0至10或自动')
        return result

    def start(self,industry,key,payload):
        record=self.knowledge.get(industry,key)
        for job in self.jobs.values():
            if (job['industry_id'],job['material_id'])==(industry,key) and job['status'] in ('queued','running'):
                return job
        job={'id':'document-'+kb.uuid4().hex[:20],'industry_id':industry,'material_id':key,
            'title':record['title'],'created_at':kb.now(),'status':'queued','input_fingerprint':input_fingerprint(record),
            'options':self.options(payload),'progress':{'processed_images':0,'tables':0},
            'note':'本机解析与识别，不代表资料已经用户核验或外部事实已确认。'}
        self.jobs[job['id']]=job;self.save_job(job);self.launch(job)
        return job

    def launch(self,job):
        self.stops[job['id']]=threading.Event()
        self.tasks[job['id']]=asyncio.create_task(self.run(job))

    def control(self,key,action):
        job=self.get(key)
        if action=='pause':
            if key in self.stops:self.stops[key].set()
            job['note']='正在暂停；当前识别步骤完成后保存断点。';self.save_job(job)
        elif action in ('resume','retry'):
            if job['status'] in ('queued','running'):return job
            current=self.knowledge.get(job['industry_id'],job['material_id'])
            if input_fingerprint(current)!=job['input_fingerprint']:raise ValueError('资料已变更，请建立新处理任务')
            job['status']='queued';self.save_job(job);self.launch(job)
        else:raise ValueError('未知图表处理操作')
        return job

    def check(self,job,model=False):
        if self.stops[job['id']].is_set():raise DocumentPaused('已保存完成步骤，可继续')
        current=self.knowledge.get(job['industry_id'],job['material_id'])
        if input_fingerprint(current)!=job['input_fingerprint']:raise ValueError('处理期间资料版本已变化，未提交新结果')
        if model and (not current.get('model_allowed') or current.get('sensitivity')=='restricted'):
            raise ValueError('资料未授权给模型，未发送图像')
        return current

    async def run(self,job):
        job['status']='running';self.save_job(job)
        try:
            await asyncio.to_thread(self.work,job)
        except DocumentPaused:
            job.update(status='interrupted',note='已保存完成步骤，等待继续。')
        except Exception as exc:
            job.update(status='failed',error=type(exc).__name__,note=str(exc)[:250] if isinstance(exc,ValueError) else '本机解析或识别未完成，原件与已有索引保留。')
        finally:self.save_job(job)

    def work(self,job):
        record=self.check(job);industry=job['industry_id'];key=job['material_id']
        path=self.knowledge.attachment(industry,key)[0] if record.get('attachment') else None
        options=job['options']
        base=self.knowledge.folder(industry)
        work=base/'text'/('structure-'+job['id'])
        work.mkdir(parents=True,exist_ok=True)
        draft_path=work/'draft.json'
        previous=load_analysis(self.store,record)
        draft=kb.read_json(draft_path) if draft_path.exists() else previous if previous and previous.get('status')!='invalidated' else {'figures':[],'tables':[]}
        parsed=parse_native(record,path,{**options,'skip_figures':[f['id'] for f in draft['figures']
            if (not options['ocr'] or f.get('ocr')) and (not options['vision'] or f.get('vision'))]})
        no_credentials(parsed['tables'])
        tables={t['id']:t for t in (previous or {}).get('tables',[]) + draft['tables']}
        for table in parsed['tables']:
            stable_table(table);tables[table['id']]=table
        figures={f['id']:f for f in (previous or {}).get('figures',[]) + draft['figures']}
        vision_used=False
        if previous and previous.get('status')!='invalidated':
            for figure in previous['figures']:
                source=self.asset(industry,key,figure['id']);target=work/figure['asset']
                if source.resolve()!=target.resolve():shutil.copyfile(source,target)
        issues=list(parsed['notices'])
        for number,image in enumerate(parsed['images'],1):
            self.check(job)
            figure_id=figure_key(image['source'])
            prior=figures.get(figure_id)
            if prior and (not options['ocr'] or prior.get('ocr')) and (not options['vision'] or prior.get('vision')):continue
            asset=figure_id+'.png';(work/asset).write_bytes(image['png'])
            figure={'id':figure_id,'caption':image['caption'],'source':image['source'],'asset':asset,
                'size':list(image['size']),'quality':'machine_recognition_needs_review'}
            if options['ocr']:
                try:
                    figure['ocr']=recognize(image['png'])
                    no_credentials(figure['ocr'])
                    for table in ruled_tables(image['png'],figure['ocr']):
                        table['source'].update(figure_id=figure_id,original_source=image['source']);stable_table(table);tables[table['id']]=table
                except Exception as exc:
                    if isinstance(exc,ValueError) and '凭据' in str(exc):raise
                    figure['ocr_error']=type(exc).__name__;issues.append('部分图像OCR未完成：'+type(exc).__name__)
            if options['vision']:
                current=self.check(job)
                if not current.get('model_allowed') or current.get('sensitivity')=='restricted':
                    figure['vision_status']='waiting_permission';issues.append('图形理解等待资料模型授权；没有发送图像')
                else:
                    try:
                        self.check(job,model=True)
                        figure['vision']=LocalVision(self.store.root).analyze(image['png'],figure.get('ocr',{}).get('text',''))
                        no_credentials(figure['vision'])
                        self.check(job,model=True)
                        vision_used=True
                        for n,shape in enumerate(figure['vision'].get('tables',[]),1):
                            if not isinstance(shape,dict) or not shape.get('cells'):continue
                            cells=[]
                            for cell in shape['cells']:
                                if not isinstance(cell,dict) or not isinstance(cell.get('text'),str):raise ValueError('图像表格单元格结构无效')
                                for field in ('row','column','row_span','column_span'):
                                    value=cell.get(field,1)
                                    if type(value) is not int or not 1<=value<=120:raise ValueError('图像表格坐标超出限制')
                                cells.append({**cell,'source':{'format':'vision','figure_id':figure_id,'original_source':image['source']}})
                            table=table_from_cells(cells,shape.get('caption') or image['caption'],shape.get('notes',[]),shape.get('header_rows',1),
                                source={'format':'vision','figure_id':figure_id,'table':n,'quality':'vision_recognition_needs_review'})
                            table['quality']='vision_recognition_needs_review';stable_table(table);tables[table['id']]=table
                    except Exception as exc:
                        # Consent/version changes must not be converted to content failures.
                        self.check(job,model=True)
                        if isinstance(exc,ValueError) and '凭据' in str(exc):raise
                        figure['vision_error']=type(exc).__name__;issues.append('部分图形理解未完成：'+type(exc).__name__)
            figures[figure_id]=figure
            job['progress']={'processed_images':len(figures),'selected_images':len(parsed['images']),'tables':len(tables)}
            kb.atomic_write(draft_path,kb.json_text({'figures':list(figures.values()),'tables':list(tables.values())}))
            self.save_job(job)
        self.check(job)
        result={'version':VERSION,'created_at':kb.now(),'input_fingerprint':job['input_fingerprint'],
            'attachment_sha256':record.get('attachment',{}).get('sha256'),'tables':list(tables.values()),
            'figures':list(figures.values()),'coverage':parsed['coverage'],'notices':list(dict.fromkeys(issues)),
            'quality':'parsed_or_recognized_not_verified','note':'表格行描述由单元格组织，OCR/视觉结果是机器识别草稿；均不等于外部事实核验或用户批准。'}
        coverage=result['coverage']
        old_coverage=draft.get('coverage',{})
        previous_coverage=(previous or {}).get('coverage',{})
        coverage['pages_covered']=sorted(set(previous_coverage.get('pages_covered',[]))|set(old_coverage.get('pages_covered',[]))|set(coverage.get('pages_covered',[])))
        if coverage['pages_total'] is not None:coverage['pages_processed']=len(coverage['pages_covered'])
        image_sources={figure_key(s):s for s in previous_coverage.get('image_sources',[])+old_coverage.get('image_sources',[])+coverage.get('image_sources',[])}
        coverage['image_sources']=list(image_sources.values());coverage['images_total']=len(image_sources)
        coverage['images_ocr']=sum(bool(f.get('ocr')) for f in figures.values())
        coverage['images_vision']=sum(bool(f.get('vision')) for f in figures.values())
        pending_pages=coverage['pages_total'] is not None and coverage['pages_processed']<coverage['pages_total']
        pending_images=[key for key in image_sources if key not in figures]
        coverage['unprocessed_images']=len(pending_images)
        coverage['next_start_page']=options.get('start_page',1) if pending_images else next((i for i in range(1,(coverage['pages_total'] or 0)+1) if i not in coverage['pages_covered']),None)
        partial=bool(pending_pages or pending_images or any((options['ocr'] and f.get('ocr_error')) or (options['vision'] and (f.get('vision_error') or f.get('vision_status'))) for f in figures.values()))
        result['status']='partial' if partial else 'completed'
        # Copy immutable native source parts; old parts and historical versions stay intact.
        native=base_record(record)
        native_relative=native.get('extraction',{}).get('text_index')
        original_parts=list(self.knowledge.index_parts(native)) if native_relative else []
        if not original_parts and record.get('content'):
            original_parts=[(0,{'text':record['content'],'first_location':'既有原文','last_location':'既有原文'})]
        parts=[]
        for _,part in original_parts:
            name=f'{len(parts)+1:05d}.json';kb.atomic_write(work/name,kb.json_text(part));parts.append({'file':name,'characters':len(part['text']),
                'first_location':part.get('first_location','原文'),'last_location':part.get('last_location','原文')})
        blocks=[]
        for table in result['tables']:
            table.pop('row_citations',None)
            blocks.extend(row_records(table))
        for figure in result['figures']:
            text=figure.get('ocr',{}).get('text','')
            if text:blocks.append({'text':'本机OCR识别文字（未核验，需核对原图）\n'+text,'location':figure['caption']+'·OCR','kind':'ocr','figure_id':figure['id']})
            vision=figure.get('vision')
            if vision:
                observations=vision.get('observations',[])
                limits=vision.get('limitations',[])
                text='本机视觉识别说明（模型识别草稿，非原生文字或事实核验）\n'+str(vision.get('summary',''))
                text+='\n观察：'+'；'.join(str(v) for v in observations[:8])+'\n限制：'+'；'.join(str(v) for v in limits[:5])
                blocks.append({'text':text,'location':figure['caption']+'·视觉识别','kind':'vision','figure_id':figure['id']})
        if sum(len(b['text']) for b in blocks)>4_000_000:raise ValueError('图表结构化文字超过400万字，请拆分处理')
        derived=index_records(blocks) if blocks else []
        for part in derived:
            number=len(parts)+1;name=f'{number:05d}.json'
            for unit in part['units']:
                if unit.get('kind')=='structured_table_row':
                    table=tables[unit['table_id']]
                    table.setdefault('row_citations',{})[str(unit['row'])]=f'{key}#block-{number}-chars-{unit["start"]+1}-{unit["end"]}'
            kb.atomic_write(work/name,kb.json_text(part));parts.append({'file':name,'characters':len(part['text']),
                'first_location':part['first_location'],'last_location':part['last_location']})
        if not parts:
            result['status']='partial';result['notices'].append('没有可索引文字；请启用OCR或图形理解后重试。')
        kb.atomic_write(work/'index.json',kb.json_text({'format':VERSION,'parts':parts,'tables':len(tables),'images':len(figures),
            'source_quality':result['quality'],'coverage':coverage}))
        kb.atomic_write(work/'result.json',kb.json_text(result))
        with kb.lock(self.store.root):
            current=self.check(job,model=vision_used)
            extraction={**current.get('extraction',{}),'native_text_index':native_relative,
                'note':'已处理本轮范围内的图表；原生表格、OCR与视觉识别表示分别标记，识别不代表核验，完整覆盖见图表报告。',
                'document_analysis':(work/'result.json').relative_to(base).as_posix(),
                'document_processing':{'status':result['status'],'version':VERSION,'coverage':coverage,'tables':len(tables)}}
            if parts:extraction['text_index']=(work/'index.json').relative_to(base).as_posix()
            self.knowledge._save(industry,current,attachment=current.get('attachment',{}),extraction=extraction)
        from .material_index import MaterialIndex
        try:MaterialIndex(self.store).build(industry,key,force=True)
        except ValueError:issues.append('没有可索引文字，原件和处理报告已保留')
        job.update(status=result['status'],note='图表处理结果已保存；识别不等于事实核验。',result_path=str((work/'result.json').relative_to(base)),
            progress={'processed_images':len(figures),'selected_images':len(parsed['images']),'tables':len(tables)})

    def analysis(self,industry,key):
        return load_analysis(self.store,self.knowledge.get(industry,key))

    def asset(self,industry,key,figure_id):
        record=self.knowledge.get(industry,key);result=load_analysis(self.store,record)
        if not result or result.get('status')=='invalidated':raise ValueError('图像识别版本不可用')
        figure=next((f for f in result['figures'] if f['id']==figure_id),None)
        if not figure or not __import__('re').fullmatch(r'figure-[0-9a-f]{12}\.png',figure['asset']):raise ValueError('图像不存在')
        base=self.knowledge.folder(industry).resolve()
        folder=(base/record['extraction']['document_analysis']).resolve().parent
        path=(folder/figure['asset']).resolve()
        if not path.is_relative_to(base/'text'):raise ValueError('图像路径无效')
        return path

    def compute(self,industry,key,payload):
        result=self.analysis(industry,key)
        if not result or result.get('status')=='invalidated':raise ValueError('请先处理当前版本的表格')
        table=next((t for t in result['tables'] if t['id']==payload.get('table_id')),None)
        if not table:raise ValueError('表格不存在')
        return calculate(table,payload.get('column'),payload.get('rows'),payload.get('operation'))

    async def close(self):
        for stop in self.stops.values():stop.set()
        if self.tasks:await asyncio.gather(*self.tasks.values(),return_exceptions=True)
        close_vision(self.store.root)
