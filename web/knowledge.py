"""Local professional materials, opt-in user memory and compact conversation history."""
from datetime import date, datetime
import hashlib
import io
from pathlib import Path
import re
import zipfile
import tempfile
from xml.etree import ElementTree as ET

from .service import kb, ws

KINDS = {'policy': '政策法规', 'plan': '规划', 'project': '项目方案', 'standard': '技术标准',
         'dataset': '资源与空间资料', 'indicator': '社会经济指标', 'case': '成果案例', 'background': '专业知识'}
STATES = {'unknown': '待核验', 'current': '现行/有效', 'upcoming': '尚未实施', 'obsolete': '已废止/被替代', 'draft': '草案/未批复'}


def clean_text(value, maximum=200000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'文本需为字符串且最多{maximum}字')
    return value.strip()


def redact(value):
    return re.sub(r'(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|Bearer\s+[A-Za-z0-9._-]{20,})', '[密钥已隐去]', value)


def normalized(value):
    return re.sub(r'[\s—–-]', '', value.casefold())


class Knowledge:
    def __init__(self, store):
        self.store = store

    def folder(self, industry):
        return self.store.home(industry) / 'materials'

    def settings(self):
        path = self.store.root / 'config/knowledge.json'
        return {'save_conversations': False, **(kb.read_json(path) if path.exists() else {})}

    def save_settings(self, payload):
        if type(payload.get('save_conversations')) is not bool:
            raise ValueError('历史对话开关需为布尔值')
        with kb.lock(self.store.root):
            kb.atomic_write(self.store.root / 'config/knowledge.json', kb.json_text({'save_conversations': payload['save_conversations']}))
        return self.settings()

    def get(self, industry, key):
        ws.safe_id(key)
        path = self.folder(industry) / (key + '.json')
        if not path.exists():
            raise ValueError('资料不存在')
        return kb.read_json(path)

    def save(self, industry, payload):
        with kb.lock(self.store.root):
            return self._save(industry, payload)

    def _save(self, industry, payload, attachment=None, extraction=None):
        """Validate and commit while the caller holds the shared workspace lock."""
        self.store.entry(industry)
        key = ws.safe_id(payload.get('id') or 'doc-' + kb.uuid4().hex[:16])
        path = self.folder(industry) / (key + '.json')
        old = kb.read_json(path) if path.exists() else {}
        record = {**old, **{k: v for k, v in payload.items() if k in (
            'title', 'kind', 'region', 'issuer', 'number', 'version', 'published_date', 'effective_date',
            'checked_date', 'review_after', 'status', 'url', 'summary', 'content', 'tags',
            'sensitivity', 'model_allowed', 'related_evidence_ids', 'notes', 'project_stage',
            'coverage_period', 'scale', 'coordinate_system', 'resolution', 'superseded_by')}}
        record.update(id=key, industry_id=industry)
        record['title'] = clean_text(record.get('title', ''), 300)
        if not record['title']:
            raise ValueError('请填写资料标题')
        record.setdefault('kind', 'background')
        record.setdefault('status', 'unknown')
        record.setdefault('sensitivity', 'internal')
        record.setdefault('model_allowed', False)
        if record['kind'] not in KINDS or record['status'] not in STATES or record['sensitivity'] not in ('public', 'internal', 'restricted'):
            raise ValueError('资料类型、状态或可见级别无效')
        if type(record['model_allowed']) is not bool:
            raise ValueError('模型引用开关需为布尔值')
        if record['sensitivity'] == 'restricted' and record['model_allowed']:
            raise ValueError('受限资料暂不提供给模型，请保留为本地管理资料')
        for field in ('region', 'issuer', 'number', 'version', 'summary', 'notes', 'project_stage', 'coverage_period', 'scale', 'coordinate_system', 'resolution', 'superseded_by'):
            record[field] = clean_text(record.get(field, ''), 3000)
        record['content'] = clean_text(record.get('content', ''))
        if redact(record['summary'] + record['content']) != record['summary'] + record['content']:
            raise ValueError('资料不保存API密钥或访问令牌，请先移除密钥')
        for field in ('published_date', 'effective_date', 'checked_date', 'review_after'):
            value = record.get(field) or None
            if value:
                date.fromisoformat(value)
                if field in ('published_date', 'checked_date') and value > datetime.now(kb.TZ).date().isoformat():
                    raise ValueError('发布/核验日期不能在未来')
            record[field] = value
        if record['status'] in ('current', 'upcoming', 'obsolete') and (not record['checked_date'] or not record.get('url')):
            raise ValueError('明确有效性状态需提供来源URL和真实核验日期；未知时选待核验')
        if record.get('url'):
            from .agent_runtime import source_url
            record['url'] = source_url(record['url'])
        else:
            record['url'] = ''
        for field in ('tags', 'related_evidence_ids'):
            values = record.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or len(v) > 100 for v in values):
                raise ValueError('标签和关联证据应为短字符串数组')
            record[field] = values
        if any(not (self.store.home(industry) / 'evidence' / (ws.safe_id(v) + '.json')).exists() for v in record['related_evidence_ids']):
            raise ValueError('关联证据不存在于当前行业')
        record['created_at'] = old.get('created_at') or kb.now()
        record['updated_at'] = kb.now()
        if attachment is not None:
            record['attachment'] = attachment
            record['extraction'] = extraction
        if old:
            kb.atomic_write(self.folder(industry) / 'history' / key / (kb.uuid4().hex + '.json'), kb.json_text(old))
        kb.atomic_write(path, kb.json_text(record))
        return self.display(record)

    @staticmethod
    def display(record):
        result = {**record, 'kind_label': KINDS[record['kind']], 'status_label': STATES[record['status']]}
        today = datetime.now(kb.TZ).date().isoformat()
        result['review_due'] = bool(record.get('review_after') and record['review_after'] <= today)
        if record['status'] == 'upcoming' and record.get('effective_date') and record['effective_date'] <= today:
            result['status_label'] = '实施日期已到，需重新核对'
            result['review_due'] = True
        result['has_attachment'] = bool(record.get('attachment'))
        return result

    def list(self, industry, query='', kind='', model_only=False):
        entries = self.store.research_entries(industry)
        tokens = [normalized(t) for t in clean_text(query, 500).split()]
        records = []
        for entry in entries:
            for path in (self.store.home(entry['id']) / 'materials').glob('*.json'):
                record = kb.read_json(path)
                if model_only and industry != 'all' and entry['id'] != industry and record.get('sensitivity') != 'public':
                    continue
                if kind and record['kind'] != kind:
                    continue
                if model_only and (not record.get('model_allowed') or record['sensitivity'] == 'restricted'):
                    continue
                text = normalized('\n'.join(str(record.get(k, '')) for k in ('title', 'summary', 'content', 'number', 'tags', 'region')))
                missing = {token for token in tokens if token not in text}
                if missing and record.get('extraction', {}).get('text_index'):
                    for _, part in self.index_parts(record):
                        body = normalized(part['text'])
                        missing = {token for token in missing if token not in body}
                        if not missing:
                            break
                if not missing:
                    records.append(self.display(record))
        return sorted(records, key=lambda r: r['updated_at'], reverse=True)

    def search(self, industry, query, limit=8):
        records = self.list(industry, query, model_only=True)
        result = []
        for record in records[:limit]:
            if record.get('extraction', {}).get('text_index'):
                candidates=[]
                terms=[normalized(t) for t in query.split()]
                for index, part in self.index_parts(record):
                    score=sum(normalized(part['text']).count(term) for term in terms)
                    candidates.append((score,index,part))
                candidates.sort(key=lambda v:(-v[0],v[1]))
                snippets=[]
                for score,index,part in candidates[:3]:
                    body=part['text']
                    # Retain the paragraph/table marker closest to a literal match.
                    hits=[]
                    for term in terms:
                        match=re.search(r'[\s—–-]*'.join(re.escape(c) for c in term),body,re.I)
                        if match:
                            hits.append(match.start())
                    position=min((p for p in hits if p>=0),default=0)
                    start=max(0,position-180)
                    marker=body.rfind('【',0,position+1)
                    if marker>=0 and position-marker<500:
                        start=marker
                    excerpt=body[start:start+1200]
                    snippets.append({'citation':f"{record['id']}#block-{index+1}-chars-{start+1}-{start+len(excerpt)}",
                        'location':part['first_location']+' 至 '+part['last_location'],'text':excerpt})
                result.append({k:record.get(k) for k in ('id','industry_id','title','kind_label','region','issuer','number',
                    'version','status_label','published_date','effective_date','checked_date','review_due','url',
                    'related_evidence_ids','notes')} | {'snippets':snippets,'citation_kind':'local-material'})
                continue
            body = record.get('content') or record.get('summary', '')
            parts = []
            for offset in range(0, len(body), 900):
                part = body[offset:offset + 1200]
                score = sum(part.casefold().count(t) for t in query.casefold().split())
                parts.append((score, offset, part))
            parts.sort(reverse=True)
            snippets = [{'citation': f"{record['id']}#chars-{offset + 1}-{offset + len(text)}", 'text': text}
                        for _, offset, text in parts[:3]]
            result.append({k: record.get(k) for k in ('id', 'industry_id', 'title', 'kind_label', 'region', 'issuer',
                'number', 'version', 'status_label', 'published_date', 'effective_date', 'checked_date', 'review_due',
                'url', 'related_evidence_ids', 'notes')} | {'snippets': snippets, 'citation_kind': 'local-material'})
        return result

    def index_parts(self, record):
        relative=record.get('extraction',{}).get('text_index')
        if not relative:
            return
        base=self.folder(record['industry_id']).resolve()
        path=(base/relative).resolve()
        if not path.is_relative_to(base/'text') or path.name!='index.json':
            raise ValueError('正文索引路径无效')
        index=kb.read_json(path)
        for n,item in enumerate(index['parts']):
            name=item['file']
            if not re.fullmatch(r'\d{5}\.json',name):
                raise ValueError('正文分块路径无效')
            part_path=(path.parent/name).resolve()
            if not part_path.is_relative_to(base/'text'):
                raise ValueError('正文分块路径无效')
            yield n,kb.read_json(part_path)

    def sections(self,industry,key,query='',offset=0,limit=5):
        tokens=[normalized(t) for t in clean_text(query,500).split()]
        if not 1<=limit<=10 or offset<0:
            raise ValueError('原文分页参数无效')
        record=self.get(industry,key)
        matches=[]
        for n,part in self.index_parts(record):
            body=normalized(part['text'])
            if all(token in body for token in tokens):
                matches.append({'index':n+1,'citation':f'{key}#block-{n+1}',**part})
        return {'total':len(matches),'offset':offset,'limit':limit,'items':matches[offset:offset+limit]}

    def import_docx(self,industry,key,source):
        """Trusted local-file import; no arbitrary server-side path endpoint is exposed."""
        from .docx_index import extract_docx,write_index,MAX_FILE_BYTES
        source=Path(source)
        if source.suffix.lower()!='.docx' or not source.is_file() or source.stat().st_size>MAX_FILE_BYTES:
            raise ValueError('本地DOCX需为100MB以内文件')
        self.get(industry,key)
        with kb.lock(self.store.root):
            folder=self.folder(industry)
            files=folder/'files'
            files.mkdir(parents=True,exist_ok=True)
            stage=files/(kb.uuid4().hex+'.tmp')
            try:
                digest=hashlib.sha256()
                length=0
                before=source.stat()
                with source.open('rb') as reader,stage.open('wb') as writer:
                    while data:=reader.read(1_048_576):
                        length+=len(data)
                        if length>MAX_FILE_BYTES:
                            raise ValueError('本地DOCX超过100MB')
                        digest.update(data)
                        writer.write(data)
                after=source.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                    raise ValueError('导入期间源文件发生变化，请重试')
                try:
                    blocks,stats=extract_docx(stage)
                except (ET.ParseError,zipfile.BadZipFile) as exc:
                    raise ValueError('DOCX结构无法读取') from exc
                if any(redact(b['text'])!=b['text'] for b in blocks):
                    raise ValueError('正文含疑似访问令牌，请先移除凭据')
                sha=digest.hexdigest()
                relative=Path('text')/(sha+'-v1')/'index.json'
                index=kb.read_json(folder/relative) if (folder/relative).exists() else write_index(folder/relative.parent,blocks,stats)
                text='\n\n'.join(f"【{b['location']}】\n{b['text']}" for b in blocks)
                if len(text)>200000:
                    text='正文与表格文字已完整建立分段索引，请使用原文检索查看片段。\n\n章节目录：\n'+\
                        '\n'.join(h['text']+'（'+h['location']+'）' for h in stats['headings'][:40])
                storage_name=key+'-'+sha+'.docx'
                stage.replace(files/storage_name)
                record=self.get(industry,key)
                return self._save(industry,{**record,'content':text},
                    attachment={'name':source.name,'suffix':'.docx','sha256':sha,'storage_name':storage_name,'bytes':length},
                    extraction={**{k:v for k,v in stats.items() if k!='headings'},'sections':len(index['parts']),
                        'text_index':relative.as_posix(),
                        'note':'正文和表格文字已分段索引；图片未做OCR。引用以段落和表格单元格定位，Word缓存页数未经渲染核对。'})
            finally:
                stage.unlink(missing_ok=True)

    def upload(self, industry, key, filename, raw):
        record = self.get(industry, key)
        suffix = Path(filename).suffix.lower()
        supported = ('.txt', '.md', '.csv', '.pdf', '.docx', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.geojson', '.zip')
        if len(raw) > 10_000_000 or suffix not in supported:
            raise ValueError('支持10MB以内文字文档、PDF、DOCX及常见图像/空间资料附件')
        if suffix=='.docx':
            scratch=self.store.root/'work'
            scratch.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(dir=scratch) as temp:
                source=Path(temp)/Path(filename).name
                source.write_bytes(raw)
                return self.import_docx(industry,key,source)
        sections = []
        if suffix in ('.txt', '.md', '.csv'):
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = raw.decode('gb18030')
            sections = [{'label': '正文', 'text': text}]
        elif suffix == '.pdf':
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw), strict=True)
            if reader.is_encrypted:
                raise ValueError('不处理加密PDF；请提供可公开读取的版本')
            if len(reader.pages) > 300:
                raise ValueError('每份PDF最多300页，请拆分资料')
            total = 0
            for i, p in enumerate(reader.pages):
                text = p.extract_text() or ''
                total += len(text)
                if total > 200000:
                    raise ValueError('正文超过20万字，请拆分资料')
                sections.append({'label': f'第{i + 1}页', 'text': text})
        text = '\n\n'.join(f"【{section['label']}】\n{section['text']}" for section in sections) if sections else record.get('content', '')
        if len(text) > 200000:
            raise ValueError('提取正文超过20万字，请拆分资料')
        digest = hashlib.sha256(raw).hexdigest()
        storage_name = key + '-' + digest + suffix
        target = self.folder(industry) / 'files' / storage_name
        with kb.lock(self.store.root):
            # Re-read metadata after parsing so a concurrent edit is not overwritten.
            current = self.get(industry, key)
            candidate = {**current, 'content': text if sections else current.get('content', '')}
            if redact(candidate['content']) != candidate['content']:
                raise ValueError('资料不保存API密钥或访问令牌，请先移除密钥')
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(suffix + '.tmp')
            temp.write_bytes(raw)
            temp.replace(target)
            return self._save(industry, candidate,
                attachment={'name': Path(filename).name, 'suffix': suffix, 'sha256': digest, 'storage_name': storage_name},
                extraction={'characters': len(candidate['content']), 'sections': len(sections),
                    'note': '仅管理原附件与元数据，未做OCR、解压或GIS空间计算。' if not sections else '未做OCR；扫描PDF可能没有可检索文字。' if suffix == '.pdf' else ''})

    def attachment(self, industry, key):
        record = self.get(industry, key)
        attachment = record.get('attachment')
        if not attachment or attachment['suffix'] not in ('.txt', '.md', '.csv', '.pdf', '.docx', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.geojson', '.zip'):
            raise ValueError('没有可下载附件')
        name = attachment.get('storage_name', key + attachment['suffix'])
        if Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError('附件路径无效')
        return self.folder(industry) / 'files' / name, attachment['name']

    def memories(self, industry, model_only=False):
        ws.selected(self.store.root, industry, many=True)
        path = self.store.root / 'data/user-memory.json'
        records = kb.read_json(path) if path.exists() else []
        return [r for r in records if not r.get('archived') and (industry == 'all' or r['industry'] in ('all', industry))
                and (not model_only or r.get('model_allowed'))]

    def save_memory(self, payload):
        industry = payload.get('industry', 'all')
        ws.selected(self.store.root, industry, many=True)
        key = ws.safe_id(payload.get('id') or 'memory-' + kb.uuid4().hex[:16])
        title, content = clean_text(payload.get('title', ''), 100), clean_text(payload.get('content', ''), 3000)
        if not title or not content or type(payload.get('model_allowed', False)) is not bool:
            raise ValueError('记忆需有标题、内容和布尔模型引用开关')
        if redact(title + content) != title + content:
            raise ValueError('记忆不保存API密钥或访问令牌')
        path = self.store.root / 'data/user-memory.json'
        with kb.lock(self.store.root):
            records = kb.read_json(path) if path.exists() else []
            old = next((r for r in records if r['id'] == key), {})
            item = {'id': key, 'industry': industry, 'title': title, 'content': content,
                    'model_allowed': payload.get('model_allowed', False), 'created_at': old.get('created_at') or kb.now(),
                    'updated_at': kb.now(), 'archived': False}
            if old:
                kb.atomic_write(self.store.root / 'data/memory-history' / key / (kb.uuid4().hex + '.json'), kb.json_text(old))
            records = [r for r in records if r['id'] != key] + [item]
            kb.atomic_write(path, kb.json_text(records))
        return item

    def archive_memory(self, key):
        ws.safe_id(key)
        path = self.store.root / 'data/user-memory.json'
        with kb.lock(self.store.root):
            records = kb.read_json(path) if path.exists() else []
            item = next((r for r in records if r['id'] == key), None)
            if not item:
                raise ValueError('记忆不存在')
            item.update(archived=True, updated_at=kb.now())
            kb.atomic_write(path, kb.json_text(records))
        return {'ok': True}

    def save_exchange(self, job, prompt, answer):
        if not self.settings()['save_conversations']:
            return
        self.store.entry(job['industry']) if job['industry'] != 'all' else None
        conversation = ws.safe_id(job['conversation'])
        key = conversation + '-' + ('all' if job['industry'] == 'all' else ws.safe_id(job['industry']))
        path = self.store.root / 'data/conversations' / (key + '.json')
        with kb.lock(self.store.root):
            record = kb.read_json(path) if path.exists() else {'id': key, 'conversation_id': conversation,
                'industry': job['industry'], 'created_at': kb.now(), 'turns': []}
            record['turns'] = [*record['turns'], {'prompt': redact(prompt), 'answer': redact(answer), 'at': kb.now()}][-50:]
            record['updated_at'] = kb.now()
            kb.atomic_write(path, kb.json_text(record))

    def conversations(self, industry):
        ws.selected(self.store.root, industry, many=True)
        records = [kb.read_json(p) for p in (self.store.root / 'data/conversations').glob('*.json')]
        return sorted([r for r in records if industry == 'all' or r['industry'] == industry], key=lambda r: r['updated_at'], reverse=True)[:50]
