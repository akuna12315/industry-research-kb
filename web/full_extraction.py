"""Durable, version-bound full-text evidence jobs. Original text stays in materials/."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from .service import kb, ws
from .knowledge import Knowledge, clean_text, redact
from .extraction_source import build_plan, split_hydrated_batch
from .extraction_model import extract_batch

VERSION = 'full-text-v1'
ACTIVE = {'queued', 'running'}
SUCCESS = {'completed', 'no_evidence'}


def digest(value):
    return hashlib.sha256(kb.json_text(value).encode()).hexdigest()


class Waiting(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class WriteBusy(ValueError):
    pass


class FullExtractionRuntime:
    def __init__(self, store, runtime):
        self.store, self.runtime = store, runtime
        self.knowledge = Knowledge(store)
        self.tasks = {}
        self.owner = kb.uuid4().hex
        self.model_override = None  # Test-only injection; never persisted.

    def folder(self, industry):
        return self.store.home(industry) / 'reports' / 'extractions'

    def path(self, industry, job_id):
        return self.folder(industry) / (ws.safe_id(job_id) + '.json')

    def _get(self, industry, job_id):
        path = self.path(industry, job_id)
        if not path.is_file():
            raise ValueError('全文提取任务不存在')
        return kb.read_json(path)

    def status_path(self, industry, job_id):
        return self.folder(industry) / 'status' / (ws.safe_id(job_id) + '.json')

    def _save(self, job):
        job['updated_at'] = kb.now()
        job['revision'] = job.get('revision', 0) + 1
        self._atomic_write(self.path(job['industry_id'], job['id']),
            json.dumps(job, ensure_ascii=False, separators=(',', ':')))
        try:
            self._atomic_write(self.status_path(job['industry_id'], job['id']), kb.json_text(self.summary(job, detail=True)))
        except OSError:
            # The small status file is a disposable cache, never a second commit.
            # Its old mtime makes readers fall back to the authoritative manifest.
            pass

    @staticmethod
    def _atomic_write(path, text):
        # Windows readers may briefly hold a handle that denies target replacement.
        for attempt in range(100):
            try:
                kb.atomic_write(path, text)
                return
            except PermissionError:
                if attempt == 99:
                    raise
                time.sleep(.025)

    @staticmethod
    def _progress(job):
        batches = [b for b in job['batches'] if b['status'] != 'split']
        done = [b for b in batches if b['status'] in SUCCESS]
        tables = set(t for b in batches for t in b.get('table_ids', []))
        processed_tables = sum(all(b['status'] in SUCCESS for b in batches if t in b.get('table_ids', [])) for t in tables)
        return {'completed': len(done), 'total': len(batches),
            'failed': sum(b['status'] == 'failed' for b in batches),
            'processed_characters': sum(b['characters'] for b in done),
            'total_characters': job['total_characters'],
            'evidence_count': len(job.get('evidence_ids', [])),
            'processed_tables': processed_tables, 'total_tables': job.get('table_count', len(tables)),
            'tables_without_text': len(job.get('tables_without_text', [])),
            'unprocessed_images': job.get('image_count', 0)}

    def summary(self, job, detail=False):
        result = {k: job.get(k) for k in ('id', 'industry_id', 'material_id', 'title', 'status',
            'created_at', 'updated_at', 'error', 'coverage_note', 'fingerprint', 'usage', 'evidence_ids',
            'provider', 'provider_history')}
        result['progress'] = self._progress(job)
        result['revision'] = job.get('revision', 0)
        result['scope_note'] = '全文文字及原生表格；图片内容未识别，不代表外部真实性已核实'
        if detail:
            result['batches'] = [{k: b.get(k) for k in ('id', 'status', 'error', 'attempts',
                'characters', 'evidence_ids', 'table_ids')} | {'index': i + 1,
                'evidence_count': len(b.get('evidence_ids', []))} for i, b in enumerate(job['batches']) if b['status'] != 'split']
            result['conflicts'] = job.get('conflicts', [])
        return result

    def get(self, industry, job_id):
        path = self.status_path(industry, job_id)
        manifest = self.path(industry, job_id)
        try:
            if path.exists() and path.stat().st_mtime_ns >= manifest.stat().st_mtime_ns:
                return kb.read_json(path)
        except (OSError, ValueError):
            pass
        return self.summary(self._get(industry, job_id), detail=True)

    def list(self, industry='all'):
        jobs = []
        for entry in self.store.research_entries(industry):
            for path in self.folder(entry['id']).glob('*.json'):
                summary = self.get(entry['id'], path.stem)
                jobs.append({k: v for k, v in summary.items() if k not in ('batches', 'conflicts')})
        return sorted(jobs, key=lambda j: (j['created_at'], j['id']), reverse=True)

    def latest(self, industry, material_id):
        jobs = [self.get(industry, p.stem) for p in self.folder(industry).glob('*.json')]
        jobs = [j for j in jobs if j['material_id'] == material_id]
        return {k: v for k, v in max(jobs, key=lambda j: (j['created_at'], j['id'])).items()
                if k not in ('batches', 'conflicts')} if jobs else None

    def _binding(self):
        cfg = self.runtime.config()
        return {k: cfg[k] for k in ('provider_id', 'base_url', 'protocol', 'model')}

    @contextmanager
    def _lock(self):
        deadline = time.monotonic() + 5
        while True:
            guard = kb.lock(self.store.root)
            try:
                guard.__enter__()
                break
            except ValueError as exc:
                if '另一轮更新正在进行' not in str(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise WriteBusy('本地库正在由其他任务写入') from None
                time.sleep(0.025)
        try:
            yield
        finally:
            guard.__exit__(None, None, None)

    def _current(self, job, model=True):
        record = self.knowledge.get(job['industry_id'], job['material_id'])
        verification = record.get('verification', {})
        if (kb.material_fingerprint(record) != job['fingerprint'] or verification.get('status') != 'reviewed'
                or verification.get('fingerprint') != job['fingerprint']):
            raise Waiting('invalidated', '资料版本或核验结果已变化，请重新核验后提取')
        if model:
            if not record.get('model_allowed') or record.get('sensitivity') == 'restricted':
                raise Waiting('waiting_permission', '资料未授权模型使用，全文提取等待授权；资料核验已保留')
            if self._binding() != job['provider']:
                raise Waiting('paused', '模型接口配置已改变，请确认设置后继续任务')
            if self.model_override is None and not self.runtime.model_key(self.runtime.config()):
                raise Waiting('waiting_model', '请在模型设置中保存密钥，再继续全文提取；资料核验已保留')
        return record

    async def create(self, industry, material_id, restart=False):
        # Source planning can parse a large attachment; never block the event loop.
        plan = await asyncio.to_thread(build_plan, self.store, industry, material_id)
        with self._lock():
            current = self.knowledge.get(industry, material_id)
            verification = current.get('verification', {})
            if verification.get('status') != 'reviewed' or verification.get('fingerprint') != plan['fingerprint']:
                raise ValueError('请先通过当前资料版本的核验')
            existing = self.latest(industry, material_id)
            if existing and existing['fingerprint'] == plan['fingerprint']:
                if existing['status'] in ACTIVE | {'waiting_model', 'waiting_permission', 'paused', 'partial'} or not restart:
                    return existing
            job = {'id': 'extract-' + kb.uuid4().hex, 'industry_id': industry, 'material_id': material_id,
                'title': current['title'], 'fingerprint': plan['fingerprint'], 'version': VERSION,
                'provider': self._binding(), 'parser_version': plan.get('parser_version'),
                'created_at': datetime.now(kb.TZ).isoformat(timespec='microseconds'), 'status': 'queued',
                'total_characters': plan['total_characters'], 'total_units': plan.get('total_units', 0),
                'table_count': plan.get('table_count', 0), 'image_count': plan.get('image_count', 0),
                'tables_without_text': plan.get('tables_without_text', []),
                'coverage_note': '；'.join(plan.get('limitations', [])), 'source_limitations': plan.get('limitations', []), 'batches': [],
                'evidence_ids': [], 'conflicts': [], 'usage': {}, 'review_id': verification.get('review_id')}
            job['provider_history'] = [{'provider': dict(job['provider']), 'at': job['created_at'], 'action': 'created'}]
            for batch in plan['batches']:
                item = {k: batch.get(k, []) for k in ('id', 'unit_ids', 'table_ids')}
                item.update(characters=batch['characters'], status='pending', attempts=0)
                if batch.get('table_structure'):
                    item['table_structure'] = batch['table_structure']
                for field in ('sources', 'context'):
                    item[field] = [{k: v for k, v in source.items() if k != 'text'}
                        | {'text_hash': digest(source['text'])} for source in batch.get(field, [])]
                job['batches'].append(item)
            if not job['batches']:
                raise ValueError('资料没有可解析的全文文字，不能创建空任务')
            try:
                self._current(job)
            except Waiting as exc:
                job.update(status=exc.status, error=str(exc))
            self._save(job)
        self._schedule(job)
        return self.summary(job)

    def _schedule(self, job):
        key = (job['industry_id'], job['id'])
        if job['status'] in ACTIVE and (key not in self.tasks or self.tasks[key].done()):
            self.tasks[key] = asyncio.create_task(self.run(*key))

    async def restore(self):
        for summary in self.list():
            if summary['status'] in ACTIVE:
                self._schedule(self._get(summary['industry_id'], summary['id']))

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def control(self, industry, job_id, action):
        if action not in ('pause', 'resume', 'retry'):
            raise ValueError('不支持的全文提取操作')
        with self._lock():
            job = self._get(industry, job_id)
            if action == 'pause':
                if job['status'] != 'completed':
                    job['status'] = 'paused'
            else:
                self._current(job, model=False)
                previous = dict(job['provider'])
                # Older manifests predate per-batch provenance. Preserve their
                # original binding before changing the model for unfinished work.
                for batch in job['batches']:
                    if batch.get('attempts') and not batch.get('provider'):
                        batch.update(provider=dict(previous), provider_inferred=True)
                if not job.get('provider_history'):
                    job['provider_history'] = [{'provider': dict(previous), 'at': job['created_at'],
                        'action': 'created', 'inferred': True}]
                current = self._binding()
                if current != previous:
                    job['provider_history'].append({'provider': dict(current), 'at': kb.now(),
                        'action': action, 'previous_provider': previous})
                    # Fence calls still in flight against the previous binding.
                    # Their old claim token can neither commit nor release a new claim.
                    for batch in job['batches']:
                        if batch['status'] == 'running':
                            batch['status'] = 'pending'
                            for field in ('owner', 'claim_id', 'lease_until'):
                                batch.pop(field, None)
                job['provider'] = current
                # Finished batches and committed evidence never re-run on resume.
                for batch in job['batches']:
                    if batch['status'] == 'failed':
                        batch.update(status='pending')
                        batch.pop('error', None)
                job['status'] = 'queued'
                job.pop('error', None)
                try:
                    self._current(job)
                except Waiting as exc:
                    job.update(status=exc.status, error=str(exc))
            self._save(job)
        key = (industry, job_id)
        if action == 'pause' and key in self.tasks:
            if not self.tasks[key].cancelling():
                self.tasks[key].cancel()
            await asyncio.gather(self.tasks[key], return_exceptions=True)
            # A cancellation can arrive while a thread is acquiring its claim.
            # All local workers have now stopped, so release those late claims.
            with self._lock():
                job = self._get(industry, job_id)
                for batch in job['batches']:
                    if batch['status'] == 'running' and batch.get('owner') == self.owner:
                        batch['status'] = 'pending'
                self._save(job)
        self._schedule(job)
        return self.summary(job)

    def _hydrate(self, record, batch):
        parts = None
        def source_text(source):
            nonlocal parts
            citation = source['citation']
            match = re.fullmatch(re.escape(record['id']) + r'#block-(\d+)-chars-(\d+)-(\d+)', citation)
            if match:
                block, start, end = map(int, match.groups())
                if parts is None:
                    parts = {n + 1: part['text'] for n, part in self.knowledge.index_parts(record)}
                body = parts.get(block, '')
            else:
                match = re.fullmatch(re.escape(record['id']) + r'#chars-(\d+)-(\d+)', citation)
                if not match:
                    raise ValueError('提取任务包含无效原文定位')
                start, end = map(int, match.groups())
                body = record.get('content') or record.get('summary', '')
            if not 1 <= start <= end <= len(body):
                raise ValueError('提取任务原文定位已失效')
            text = body[start - 1:end]
            if digest(text) != source['text_hash']:
                raise ValueError('提取任务原文内容已变化，未发送模型')
            return {**source, 'text': text}
        return {**batch, **{field: [source_text(s) for s in batch.get(field, [])] for field in ('sources', 'context')}}

    def _claim(self, industry, job_id):
        with self._lock():
            job = self._get(industry, job_id)
            if job['status'] not in ACTIVE:
                return None
            self._current(job)
            now = datetime.now(kb.TZ)
            for batch in job['batches']:
                if batch['status'] == 'running' and datetime.fromisoformat(batch['lease_until']) < now:
                    batch['status'] = 'pending'
                if batch['status'] == 'pending':
                    batch.update(status='running', owner=self.owner, claim_id=kb.uuid4().hex, attempts=batch['attempts'] + 1,
                        lease_until=(now + timedelta(seconds=90)).isoformat(), provider=dict(job['provider']))
                    batch.pop('provider_inferred', None)
                    job['status'] = 'running'
                    self._save(job)
                    return job, dict(batch)
            return None

    def _split(self, job, batch):
        hydrated = self._hydrate(self._current(job), batch)
        children = split_hydrated_batch(hydrated)
        batch['status'] = 'split'
        items = []
        for child in children:
            item = {k: v for k, v in child.items() if k not in ('sources', 'context', 'owner', 'claim_id', 'lease_until')}
            item.update(status='pending', attempts=0)
            for field in ('sources', 'context'):
                item[field] = [{k: v for k, v in source.items() if k != 'text'}
                    | {'text_hash': digest(source['text'])} for source in child.get(field, [])]
            items.append(item)
        index = job['batches'].index(batch) + 1
        job['batches'][index:index] = items

    def _commit(self, industry, job_id, batch_id, analysis, claim_id=None):
        from .verification import VerificationQueue
        with self._lock():
            job = self._get(industry, job_id)
            batch = next(b for b in job['batches'] if b['id'] == batch_id)
            if (job['status'] not in ACTIVE or batch['status'] != 'running' or batch.get('owner') != self.owner
                    or (claim_id is not None and batch.get('claim_id') != claim_id)):
                return
            material = self._current(job)
            for field, value in analysis.get('usage', {}).items():
                if isinstance(value, (int, float)):
                    job['usage'][field] = job['usage'].get(field, 0) + value
            if not analysis.get('complete') or analysis.get('needs_split'):
                self._split(job, batch)
                self._save(job)
                return
            home = self.store.home(industry)
            candidates = analysis.get('candidates', [])
            if not candidates and not analysis.get('processed_no_evidence'):
                raise ValueError('空提取结果没有说明已完整处理，不能记为成功')
            records = []
            for candidate in candidates:
                supports = candidate.get('supports') or [{'citation': candidate['citation'], 'quote': candidate['quote']}]
                allowed = {s['citation'] for s in batch['sources'] + batch.get('context', [])}
                if candidate['citation'] not in {s['citation'] for s in batch['sources']}:
                    raise ValueError('主引用必须来自本批完整原文')
                for support in supports + [{'citation': candidate['citation'], 'quote': candidate['quote']}]:
                    if support['citation'] not in allowed or not kb.material_quote_matches(home, material, support):
                        raise ValueError('证据引用或辅助原文与当前资料不匹配')
                semantic = candidate.get('semantic') or {'quote': candidate['quote']}
                semantic = {str(k): re.sub(r'\s+', '', str(v)).casefold() for k, v in semantic.items()}
                stable = digest([industry, material['id'], job['fingerprint'], semantic])[:32]
                evidence_id = 'fact-' + stable
                source = {'industry_id': industry, 'material_id': material['id'], 'fingerprint': job['fingerprint'],
                    'attachment_sha256': material.get('attachment', {}).get('sha256'),
                    'citation': candidate['citation'], 'quote': candidate['quote']}
                path = home / 'evidence' / (evidence_id + '.json')
                old = kb.read_json(path) if path.exists() else {}
                if old and old.get('material_source', {}).get('fingerprint') != job['fingerprint']:
                    raise ValueError('证据标识与其他资料版本冲突')
                references = old.get('supporting_sources', []) + supports
                references = list({(s['citation'], s['quote']): s for s in references}.values())
                record = {**old, **{k: candidate[k] for k in ('title', 'summary', 'category', 'region', 'status')},
                    'id': evidence_id, 'url': '', 'publisher': material.get('issuer') or '用户提供的本地资料',
                    'source_kind': 'local_material', 'material_source': old.get('material_source') or source,
                    'supporting_sources': references, 'semantic': semantic, 'extraction_method': 'full_text_model',
                    'extraction_job_id': job['id'], 'extraction_provider': dict(batch.get('provider') or job['provider']),
                    'sensitivity': material['sensitivity'],
                    'model_allowed': material['model_allowed'], 'checked_date': datetime.now(kb.TZ).date().isoformat(),
                    'limitations': '来自已通过资料的全文文字提取；模型校验不等于外部真实性、批复或实绩确认。'}
                if record['status'] not in ('reported', 'planned', 'needs_verification'):
                    raise ValueError('全文提取不能自行确认外部事实')
                if redact(record['summary']) != record['summary']:
                    raise ValueError('提取结果含疑似凭据，未入库')
                records.append(kb.validate(record))
            # Keep incompatible values side by side instead of choosing a winner.
            previous_records = []
            for evidence_id in job['evidence_ids']:
                evidence_path = home / 'evidence' / (evidence_id + '.json')
                if evidence_path.exists():
                    previous_records.append(kb.read_json(evidence_path))
            for record in records:
                scope = {k: v for k, v in record['semantic'].items() if k != 'object'}
                for previous in previous_records:
                    other = previous.get('semantic', {})
                    if (scope and scope == {k: v for k, v in other.items() if k != 'object'}
                            and record['semantic'].get('object') != other.get('object')):
                        pair = sorted([record['id'], previous['id']])
                        if pair not in job['conflicts']:
                            job['conflicts'].append(pair)
                previous_records.append(record)
            # A duplicate fact within one batch merges source references.
            merged = {}
            for record in records:
                if record['id'] in merged:
                    previous = merged[record['id']]
                    previous['supporting_sources'] = list({(s['citation'], s['quote']): s
                        for s in previous['supporting_sources'] + record['supporting_sources']}.values())
                else:
                    merged[record['id']] = record
            records = list(merged.values())
            material_path = self.knowledge.folder(industry) / (material['id'] + '.json')
            watched = [self.path(industry, job_id), self.status_path(industry, job_id), material_path,
                home / 'data/search.sqlite3', home / 'logs/imports.jsonl']
            for record in records:
                watched += [home / 'evidence' / (record['id'] + suffix) for suffix in ('.json', '.md')]
            snapshots = {p: p.read_bytes() if p.exists() else None for p in watched}
            history_folders = [home / 'history' / r['id'] for r in records]
            history_folders.append(self.knowledge.folder(industry) / 'history' / material['id'])
            histories = {p for folder in history_folders for p in folder.glob('*.json')}
            try:
                if records:
                    kb.import_records(home, records, already_locked=True)
                ids = [r['id'] for r in records]
                job['evidence_ids'] = list(dict.fromkeys(job['evidence_ids'] + ids))
                batch.update(status='completed' if records else 'no_evidence', evidence_ids=ids)
                batch.pop('error', None)
                verification = {**material['verification'], 'extraction_status': 'pending',
                    'extraction_method': 'full_text_model', 'extraction_job_id': job_id}
                verification.pop('extraction_error', None)
                self.knowledge._save(industry, {**material, 'related_evidence_ids': list(dict.fromkeys(
                    material.get('related_evidence_ids', []) + ids))}, verification=verification)
                self._save(job)
            except Exception:
                VerificationQueue._rollback(snapshots, history_folders, histories)
                raise

    def _finish(self, industry, job_id):
        with self._lock():
            job = self._get(industry, job_id)
            if job['status'] not in ACTIVE:
                return
            self._current(job)
            batches = [b for b in job['batches'] if b['status'] != 'split']
            if any(b['status'] in ('pending', 'running') for b in batches):
                return
            job['status'] = 'partial' if any(b['status'] == 'failed' for b in batches) else 'completed'
            progress = self._progress(job)
            if job['status'] == 'completed' and progress['processed_characters'] != progress['total_characters']:
                job.update(status='partial', error='原文覆盖字符数不一致，不能报告全文处理完成')
            record = self._current(job)
            verification = {**record['verification'], 'extraction_status': 'failed' if job['status'] == 'partial' else 'completed',
                'extraction_method': 'full_text_model', 'extraction_job_id': job_id, 'evidence_ids': job['evidence_ids']}
            if job['status'] == 'partial':
                verification['extraction_error'] = '部分全文批次失败，已入库成果保留，可重试失败项'
            else:
                verification.pop('extraction_error', None)
                job['coverage_note'] = '已规划的全文文字及有原生文字的表格处理完成；' + (
                    f'{job["image_count"]}个图片媒体内容尚未识别；' if job['image_count'] else '') + (
                    f'{len(job["tables_without_text"])}张无原生文字表格未作视觉识别；' if job.get('tables_without_text') else '')
                job['coverage_note'] += '；'.join(job.get('source_limitations', [])) + '；不代表事实零遗漏或外部真实性已核实。'
            self.knowledge._save(industry, record, verification=verification)
            self._save(job)

    async def run(self, industry, job_id):
        provider = self._get(industry, job_id)['provider']
        hostname = urlsplit(provider.get('base_url', '')).hostname
        serial = hostname in ('127.0.0.1', 'localhost', '::1') or (
            hostname == 'open.bigmodel.cn' and provider.get('model') == 'glm-4.7-flash')
        workers = [asyncio.create_task(self._worker(industry, job_id)) for _ in range(1 if serial else 3)]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                if not worker.done() and not worker.cancelling():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(self, industry, job_id):
        current_batch = None
        current_claim = None
        try:
            while True:
                claimed = await self._transaction(self._claim, industry, job_id)
                if not claimed:
                    job = self._get(industry, job_id)
                    if job['status'] in ACTIVE and any(b['status'] == 'running' for b in job['batches']):
                        await asyncio.sleep(5)
                        continue
                    break
                job, batch = claimed
                current_batch = batch['id']
                current_claim = batch['claim_id']
                analysis = None
                try:
                    record = self._current(job)
                    hydrated = await asyncio.to_thread(self._hydrate, record, batch)
                    material = {k: record.get(k) for k in ('id', 'industry_id', 'title', 'kind', 'region', 'version', 'project_stage')}
                    material['fingerprint'] = job['fingerprint']
                    heartbeat = asyncio.create_task(self._heartbeat(industry, job_id, batch['id'], batch['claim_id']))
                    try:
                        analysis = await extract_batch(self.runtime, material, hydrated, model_override=self.model_override)
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
                    committing = asyncio.create_task(self._transaction(self._commit, industry, job_id,
                        batch['id'], analysis, batch['claim_id']))
                    try:
                        await asyncio.shield(committing)
                    except asyncio.CancelledError:
                        await committing
                        raise
                except Waiting:
                    raise
                except Exception as exc:
                    message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    usage = (analysis or {}).get('usage', getattr(exc, 'usage', {}))
                    if getattr(exc, 'retry_later', False):
                        await self._transaction(self._release, industry, job_id, batch['id'],
                            batch['claim_id'], 'paused', message, usage)
                        return
                    await self._transaction(self._fail_batch, industry, job_id, batch['id'], batch['claim_id'], message, usage)
                current_batch = None
                current_claim = None
                await asyncio.sleep(0)
            await self._transaction(self._finish, industry, job_id)
        except Waiting as exc:
            await self._transaction(self._release, industry, job_id, current_batch, current_claim, exc.status, str(exc))
        except asyncio.CancelledError:
            await self._transaction(self._release, industry, job_id, current_batch, current_claim, None, None)
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            await self._transaction(self._release, industry, job_id, current_batch, current_claim, 'partial', message)

    async def _transaction(self, function, *args):
        read_retries = 0
        while True:
            try:
                operation = asyncio.create_task(asyncio.to_thread(function, *args))
                try:
                    return await asyncio.shield(operation)
                except asyncio.CancelledError:
                    # A thread cannot be cancelled. Wait for its mutation before
                    # pause releases claims, so it cannot claim again after resume.
                    while not operation.done():
                        try:
                            await asyncio.shield(operation)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if operation.done() and not operation.cancelled():
                        operation.exception()
                    raise
            except PermissionError:
                read_retries += 1
                if read_retries >= 20:
                    raise
                await asyncio.sleep(.05)
            except ValueError as exc:
                if not isinstance(exc, WriteBusy) and '另一轮更新正在进行' not in str(exc):
                    raise
                # File-lock contention is neither a model error nor a failed batch.
                await asyncio.sleep(.2)

    def _fail_batch(self, industry, job_id, batch_id, claim_id, message, usage=None):
        with self._lock():
            job = self._get(industry, job_id)
            target = next(b for b in job['batches'] if b['id'] == batch_id)
            if (target['status'] == 'running' and target.get('owner') == self.owner
                    and target.get('claim_id') == claim_id):
                target.update(status='failed', error=redact(message[:1500]))
                for field, value in (usage or {}).items():
                    if isinstance(value, (int, float)):
                        job['usage'][field] = job['usage'].get(field, 0) + value
                self._save(job)

    def _release(self, industry, job_id, batch_id, claim_id, status, message, usage=None):
        with self._lock():
            job = self._get(industry, job_id)
            if batch_id:
                target = next(b for b in job['batches'] if b['id'] == batch_id)
                if target.get('owner') != self.owner or target.get('claim_id') != claim_id:
                    return
                if target['status'] == 'running':
                    target['status'] = 'pending'
            elif any(b['status'] == 'running' and b.get('owner') != self.owner for b in job['batches']):
                return
            for field, value in (usage or {}).items():
                if isinstance(value, (int, float)):
                    job['usage'][field] = job['usage'].get(field, 0) + value
            if status:
                job.update(status=status, error=redact(message[:1500]))
            elif job['status'] in ACTIVE:
                job['status'] = 'queued'
            self._save(job)

    async def _heartbeat(self, industry, job_id, batch_id, claim_id):
        while True:
            await asyncio.sleep(20)
            if not await self._transaction(self._renew, industry, job_id, batch_id, claim_id):
                return

    def _renew(self, industry, job_id, batch_id, claim_id):
        with self._lock():
            job = self._get(industry, job_id)
            batch = next(b for b in job['batches'] if b['id'] == batch_id)
            if (job['status'] not in ACTIVE or batch.get('owner') != self.owner
                    or batch.get('claim_id') != claim_id or batch['status'] != 'running'):
                return False
            batch['lease_until'] = (datetime.now(kb.TZ) + timedelta(seconds=90)).isoformat()
            self._save(job)
            return True
