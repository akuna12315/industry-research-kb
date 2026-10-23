"""Human confirmation queue for bounded material and public-source fact drafts."""
from datetime import datetime
import hashlib

from .knowledge import Knowledge, clean_text, redact
from .service import kb, ws


class VerificationQueue:
    def __init__(self, store):
        self.store = store
        self.knowledge = Knowledge(store)

    def folder(self, industry):
        return self.store.home(industry) / 'reports' / 'verifications'

    def path(self, industry, key):
        return self.folder(industry) / (ws.safe_id(key) + '.json')

    def material_context(self, industry, material_id):
        record = self.knowledge.get(industry, material_id)
        excerpts = []
        if record.get('extraction', {}).get('text_index'):
            parts = list(self.knowledge.index_parts(record))
            indexes = sorted({round(i * (len(parts) - 1) / 5) for i in range(6)}) if parts else []
            for index in indexes:
                n, part = parts[index]
                text = part['text'][:1600]
                if text.strip():
                    excerpts.append({'citation': f'{material_id}#block-{n + 1}-chars-1-{len(text)}',
                        'location': part['first_location'] + ' 至 ' + part['last_location'], 'text': text})
            note = f'仅抽样 {len(excerpts)} 个正文分段的开头片段，共有 {len(parts)} 个分段；不代表全文核验，图片未做OCR。'
        else:
            body = record.get('content') or record.get('summary', '')
            offsets = sorted({round(i * max(0, len(body) - 1600) / 5) for i in range(6)}) if body else []
            for offset in offsets:
                text = body[offset:offset + 1600]
                if text.strip():
                    excerpts.append({'citation': f'{material_id}#chars-{offset + 1}-{offset + len(text)}',
                        'location': f'文字位置 {offset + 1}–{offset + len(text)}', 'text': text})
            note = f'提供 {len(excerpts)} 个有界文字片段，不代表整份资料及项目事实已核验。'
        material = {key: record.get(key) for key in ('id', 'industry_id', 'title', 'kind', 'region', 'issuer',
            'number', 'version', 'published_date', 'effective_date', 'status', 'project_stage', 'sensitivity',
            'model_allowed', 'url')}
        return {'material': material, 'excerpts': excerpts, 'fingerprint': kb.material_fingerprint(record),
            'coverage_note': note}

    def _candidates(self, industry, candidates, excerpts):
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 12:
            raise ValueError('核验需包含1至12条事实候选')
        citations = {part['citation']: part['text'] for part in excerpts}
        topics = ws.scope(self.store.home(industry)).get('topics', [])
        result = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError('事实候选需为对象')
            clean = {key: clean_text(candidate.get(key, ''), maximum) for key, maximum in
                (('title', 300), ('summary', 3000), ('quote', 1200), ('citation', 300), ('category', 200), ('region', 200))}
            if any(not value for value in clean.values()):
                raise ValueError('候选标题、摘要、逐字摘录、定位、分类与地域不可为空')
            if clean['citation'] not in citations or clean['quote'] not in citations[clean['citation']]:
                raise ValueError('逐字摘录必须出自该引用位置的已读取片段')
            if redact(clean['quote'] + clean['summary']) != clean['quote'] + clean['summary']:
                raise ValueError('核验候选不可包含访问令牌')
            if topics and clean['category'] not in topics:
                raise ValueError('候选分类不属于当前行业范围')
            clean['status'] = candidate.get('status', 'needs_verification')
            if clean['status'] not in ('reported', 'planned', 'needs_verification'):
                raise ValueError('候选只能标记来源声称、计划或待核验，不能自动标记交叉确认')
            result.append(clean)
        return result

    def _write(self, industry, review):
        path = self.path(industry, review['id'])
        if path.exists():
            kb.atomic_write(self.folder(industry) / 'history' / review['id'] / (kb.uuid4().hex + '.json'),
                kb.json_text(kb.read_json(path)))
        kb.atomic_write(path, kb.json_text(review))
        return review

    def prepare_material(self, industry, material_id, mode='manual', analysis=None):
        if mode not in ('manual', 'automatic'):
            raise ValueError('核验方式需为manual或automatic')
        context = self.material_context(industry, material_id)
        material = context['material']
        if mode == 'automatic' and (not material.get('model_allowed') or material.get('sensitivity') == 'restricted'):
            raise ValueError('资料尚未授权模型引用，不能自动分析')
        if not context['excerpts']:
            raise ValueError('资料没有可核验的文字片段，请先提供原文')
        if mode == 'automatic' and not isinstance(analysis, dict):
            raise ValueError('自动核验需先取得基于授权片段的分析结果')
        if analysis is None:
            first = context['excerpts'][0]
            quote = first['text'][:500].strip()
            scope = ws.scope(self.store.home(industry))
            analysis = {'candidates': [{'title': '资料摘录：' + material['title'][:270],
                'summary': quote, 'quote': quote, 'citation': first['citation'], 'status': 'needs_verification',
                'category': scope['topics'][0], 'region': material.get('region') or scope.get('primary_region') or '未指定'}],
                'findings': ['以下为逐字摘录草稿，请核对事实、计划性质与适用范围后确认；确认不代表项目已经批复或实施。']}
        candidates = self._candidates(industry, analysis.get('candidates'), context['excerpts'])
        findings = analysis.get('findings', [])
        if not isinstance(findings, list) or len(findings) > 20:
            raise ValueError('核验发现需为最多20项的文字数组')
        findings = [clean_text(value, 2000) for value in findings]
        with kb.lock(self.store.root):
            record = self.knowledge.get(industry, material_id)
            if kb.material_fingerprint(record) != context['fingerprint']:
                raise ValueError('资料已变更，请重新生成核验草稿')
            for path in self.folder(industry).glob('*.json'):
                previous = kb.read_json(path)
                if previous.get('material_id') == material_id and previous['status'] == 'pending' and previous['mode'] == mode and previous['source']['fingerprint'] == context['fingerprint']:
                    return previous
            source = {'kind': 'material', 'material_id': material_id, 'industry_id': industry,
                'fingerprint': context['fingerprint'], 'url': record.get('url', ''), 'title': record['title'],
                'attachment_sha256': record.get('attachment', {}).get('sha256')}
            review = {'id': 'review-' + kb.uuid4().hex, 'industry_id': industry, 'material_id': material_id,
                'title': material['title'], 'status': 'pending', 'mode': mode, 'source': source,
                'excerpts': context['excerpts'], 'candidates': candidates, 'findings': findings,
                'coverage_note': context['coverage_note'], 'created_at': kb.now()}
            if record.get('verification', {}).get('status') == 'reviewed':
                review['previous_verification'] = record['verification']
            if analysis.get('coverage_note'):
                review['coverage_note'] += '\n自动分析限制：' + clean_text(analysis['coverage_note'], 3000)
            self._write(industry, review)
            self.knowledge._save(industry, record, verification={'status': 'pending', 'review_id': review['id'],
                'fingerprint': context['fingerprint'], 'prepared_at': review['created_at']})
            return review

    def prepare_web(self, industry, record, page):
        self.store.entry(industry)
        record = {key: value for key, value in record.items() if key in (
            'id', 'title', 'summary', 'url', 'publisher', 'region', 'category', 'status', 'tags',
            'published_date', 'event_date', 'source_type', 'source_id', 'review_after', 'valid_until',
            'superseded_by', 'metrics', 'limitations', 'quote', 'citation')}
        if record.get('url') not in (page.get('url'), page.get('requested_url')):
            raise ValueError('网页候选必须对应本轮实际读取的原始URL')
        quote = clean_text(record.get('quote', ''), 1200)
        text = page.get('text', '')
        if not quote or quote not in text:
            raise ValueError('网页候选必须附带已读取原文中的逐字摘录')
        start = max(0, text.index(quote) - 200)
        excerpt = text[start:start + max(1600, len(quote) + 200)]
        citation = record.get('citation') or f"{page['url']}#chars-{start + 1}-{start + len(excerpt)}"
        excerpts = [{'citation': citation, 'location': f'原文文字位置 {start + 1}–{start + len(excerpt)}', 'text': excerpt}]
        candidates = self._candidates(industry, [{**record, 'citation': citation}], excerpts)
        clean = kb.validate({**record, 'checked_date': page['checked_date']})
        material_id = 'web-' + hashlib.sha256(page['url'].encode()).hexdigest()[:32]
        material_path = self.knowledge.folder(industry) / (material_id + '.json')
        baseline = kb.material_fingerprint(kb.read_json(material_path)) if material_path.exists() else None
        review = {'id': 'review-' + kb.uuid4().hex, 'industry_id': industry, 'material_id': None,
            'title': clean['title'], 'status': 'pending', 'mode': 'automatic',
            'source': {'kind': 'public_web', 'url': page['url'], 'title': page.get('title') or clean['title'],
                'fingerprint': hashlib.sha256((page['url'] + '\n' + text).encode()).hexdigest(),
                'checked_date': page['checked_date'], 'publisher': clean['publisher'],
                'target_material_id': material_id, 'material_fingerprint_at_prepare': baseline},
            'excerpts': excerpts, 'candidates': candidates, 'findings': ['公开原文已读取，事实草稿需由用户确认后才入库。'],
            'coverage_note': '仅保留支持候选的短摘录；来源声称不等于已交叉确认。', 'created_at': kb.now(),
            'web_record': clean}
        with kb.lock(self.store.root):
            return self._write(industry, review)

    def _project(self, industry, review):
        result = dict(review)
        if review.get('material_id') and review['status'] in ('pending', 'approved'):
            try:
                current = self.knowledge.get(industry, review['material_id'])
                if review['source']['kind'] == 'public_web':
                    verification = current.get('verification', {})
                    changed = verification.get('status') == 'invalidated' or verification.get('public_source_fingerprint') != review['source']['fingerprint']
                else:
                    changed = kb.material_fingerprint(current) != review['source']['fingerprint']
            except ValueError:
                changed = True
            if changed:
                result.update(status='invalidated', material_verified=False, invalidation_reason='资料版本已变化，请重新核验')
            elif review['status'] == 'approved':
                from .material_index import MaterialIndex
                indexed = MaterialIndex(self.store).latest(industry, review['material_id'])
                if indexed and indexed['status'] == 'completed':
                    result.update(extraction_status='indexed', extraction_job=indexed)
        elif review['source']['kind'] == 'public_web' and review['status'] == 'pending':
            material_id = review['source'].get('target_material_id')
            if material_id:
                path = self.knowledge.folder(industry) / (material_id + '.json')
                material = kb.read_json(path) if path.exists() else None
                current = kb.material_fingerprint(material) if material else None
                same_public_version = material and material.get('verification', {}).get('status') != 'invalidated' and material.get('verification', {}).get('public_source_fingerprint') == review['source']['fingerprint']
                if current != review['source'].get('material_fingerprint_at_prepare') and not same_public_version:
                    result.update(status='invalidated', material_verified=False, invalidation_reason='同来源资料已有新的核验版本，请重新核验')
        return result

    def get(self, industry, key):
        path = self.path(industry, key)
        if not path.exists():
            raise ValueError('核验记录不存在')
        return self._project(industry, kb.read_json(path))

    def list(self, industry='all', status='pending'):
        if status not in ('', 'all', 'pending', 'approved', 'rejected', 'invalidated'):
            raise ValueError('核验状态无效')
        results = []
        for entry in self.store.research_entries(industry, include_shared=True):
            for path in self.folder(entry['id']).glob('*.json'):
                review = self._project(entry['id'], kb.read_json(path))
                if status in ('', 'all') or review['status'] == status:
                    results.append(review)
        return sorted(results, key=lambda review: review['created_at'], reverse=True)

    def approve_material(self, industry, key, payload):
        """Record the user's material decision before any evidence extraction."""
        note = clean_text(payload.get('note', ''), 3000)
        with kb.lock(self.store.root):
            path = self.path(industry, key)
            if not path.exists():
                raise ValueError('核验记录不存在')
            review = kb.read_json(path)
            projected = self._project(industry, review)
            if projected['status'] == 'invalidated':
                self._write(industry, {**projected, 'invalidated_at': kb.now()})
                raise ValueError('资料版本已变化，旧核验结果不能批准，请重新核验')
            if review['source']['kind'] != 'material':
                raise ValueError('此操作仅适用于已导入的资料')
            retrying = review['status'] == 'approved' and review.get('extraction_status') == 'failed'
            if review['status'] == 'approved' and not retrying:
                return review
            if review['status'] != 'pending' and not retrying:
                raise ValueError('此核验已经处理，不能再次改变决定')
            record = self.knowledge.get(industry, review['material_id'])
            result = {**review, 'status': 'approved', 'decision_note': note or review.get('decision_note', ''),
                'decided_at': review.get('decided_at') or kb.now(),
                'material_verified': True, 'extraction_status': 'pending', 'evidence_ids': []}
            result.pop('extraction_error', None)
            verification = {'status': 'reviewed', 'review_id': key,
                'fingerprint': review['source']['fingerprint'], 'reviewed_at': result['decided_at'],
                'evidence_ids': [], 'note': result['decision_note'], 'extraction_status': 'pending'}
            material_path = self.knowledge.folder(industry) / (record['id'] + '.json')
            snapshots = {item: item.read_bytes() for item in (path, material_path)}
            history_folders = [self.folder(industry) / 'history' / key,
                self.knowledge.folder(industry) / 'history' / record['id']]
            histories = {item for folder in history_folders for item in folder.glob('*.json')}
            try:
                self.knowledge._save(industry, record, verification=verification)
                self._write(industry, result)
            except Exception:
                self._rollback(snapshots, history_folders, histories)
                raise
            return result

    @staticmethod
    def _rollback(snapshots, history_folders, histories):
        for item, data in snapshots.items():
            if data is None:
                item.unlink(missing_ok=True)
            else:
                item.parent.mkdir(parents=True, exist_ok=True)
                temp = item.with_name(item.name + '.rollback-' + kb.uuid4().hex)
                temp.write_bytes(data)
                temp.replace(item)
        for folder in history_folders:
            for item in folder.glob('*.json'):
                if item not in histories:
                    item.unlink()

    def complete_material_extraction(self, industry, key, analysis):
        if not isinstance(analysis, dict):
            raise ValueError('自动提取结果格式无效')
        return self.decide(industry, key, {'decision': 'approve', 'candidates': analysis.get('candidates')}, analysis)

    def extraction_failed(self, industry, key, error):
        """Keep material consent while exposing an extraction failure for retry."""
        with kb.lock(self.store.root):
            review = self.get(industry, key)
            if review['status'] != 'approved' or review.get('extraction_status') in ('completed', 'indexed'):
                return review
            result = {**review, 'material_verified': True, 'extraction_status': 'failed',
                'extraction_error': redact(clean_text(error, 3000)), 'extraction_finished_at': kb.now()}
            record = self.knowledge.get(industry, review['material_id'])
            verification = {**record.get('verification', {}), 'extraction_status': 'failed',
                'extraction_error': result['extraction_error']}
            material_path = self.knowledge.folder(industry) / (record['id'] + '.json')
            snapshots = {item: item.read_bytes() for item in (self.path(industry, key), material_path)}
            history_folders = [self.folder(industry) / 'history' / key,
                self.knowledge.folder(industry) / 'history' / record['id']]
            histories = {item for folder in history_folders for item in folder.glob('*.json')}
            try:
                if record.get('verification', {}).get('review_id') == key:
                    self.knowledge._save(industry, record, verification=verification)
                return self._write(industry, result)
            except Exception:
                self._rollback(snapshots, history_folders, histories)
                raise

    def decide(self, industry, key, payload, analysis=None):
        if not isinstance(payload, dict) or payload.get('decision') not in ('approve', 'reject'):
            raise ValueError('请明确批准或拒绝核验结果')
        note = clean_text(payload.get('note', ''), 3000)
        with kb.lock(self.store.root):
            path = self.path(industry, key)
            if not path.exists():
                raise ValueError('核验记录不存在')
            review = kb.read_json(path)
            projected = self._project(industry, review)
            if projected['status'] == 'invalidated':
                self._write(industry, {**projected, 'invalidated_at': kb.now()})
                raise ValueError('资料版本已变化，旧核验结果不能批准，请重新核验')
            expected = 'approved' if payload['decision'] == 'approve' else 'rejected'
            extracting = (review['status'] == 'approved' and payload['decision'] == 'approve'
                and review.get('extraction_status') in ('pending', 'failed'))
            if review['status'] == expected and not extracting:
                return review
            if review['status'] != 'pending' and not extracting:
                raise ValueError('此核验已经处理，不能再次改变决定')
            if payload['decision'] == 'reject':
                result = {**review, 'status': 'rejected', 'decision_note': note, 'decided_at': kb.now(), 'evidence_ids': []}
                self._write(industry, result)
                if review.get('material_id'):
                    record = self.knowledge.get(industry, review['material_id'])
                    current_verification = record.get('verification', {})
                    if current_verification.get('review_id') == review['id']:
                        restored = review.get('previous_verification') or {'status': 'unverified',
                            'review_id': review['id'], 'fingerprint': review['source']['fingerprint'], 'decision': 'rejected'}
                        self.knowledge._save(industry, record, verification=restored)
                return result
            candidates = self._candidates(industry, payload.get('candidates', review['candidates']), review['excerpts'])
            if analysis is not None:
                review = {**review, 'extraction_method': clean_text(analysis.get('extraction_method', 'model'), 100)}
                if analysis.get('coverage_note') and review['mode'] != 'automatic':
                    review['coverage_note'] += '\n自动提取范围：' + clean_text(analysis['coverage_note'], 3000)
            local_source = review['source']['kind'] == 'material'
            if not local_source:
                from .agent_runtime import allowed_source
                if not allowed_source(review['source']['url'], self.store.sources(industry)):
                    raise ValueError('该来源已不在当前允许范围内，不能批准归档')
            record = self.knowledge.get(industry, review['material_id']) if local_source else None
            web_material = None
            if not local_source:
                material_id = 'web-' + hashlib.sha256(review['source']['url'].encode()).hexdigest()[:32]
                material_path = self.knowledge.folder(industry) / (material_id + '.json')
                existing_material = self.knowledge.get(industry, material_id) if material_path.exists() else {}
                if existing_material and existing_material.get('url') != review['source']['url']:
                    raise ValueError('网页资料标识与其他来源冲突')
                web_material = {**existing_material, 'id': material_id, 'title': review['source']['title'],
                    'kind': existing_material.get('kind', 'background'), 'region': candidates[0]['region'],
                    'issuer': review['source']['publisher'], 'url': review['source']['url'],
                    'summary': review['web_record']['summary'], 'content': '\n\n'.join(item['text'] for item in review['excerpts']),
                    'sensitivity': existing_material.get('sensitivity', 'public'),
                    'model_allowed': existing_material.get('model_allowed', True),
                    'notes': '公开原文的短摘录，完整原文请打开来源。' + review['coverage_note']}
                same_public_version = existing_material.get('verification', {}).get('public_source_fingerprint') == review['source']['fingerprint']
                if same_public_version:
                    excerpts = [existing_material.get('content', '')] + [item['text'] for item in review['excerpts']]
                    web_material['content'] = '\n\n'.join(dict.fromkeys(text for text in excerpts if text))[:200000]
                record = self.knowledge._save(industry, web_material, validate_only=True)
                if same_public_version:
                    record['related_evidence_ids'] = existing_material.get('related_evidence_ids', [])
            records = []
            for index, candidate in enumerate(candidates):
                if local_source:
                    identity = '\n'.join((industry, record['id'], candidate['citation'], candidate['title']))
                    evidence_id = 'local-' + hashlib.sha256(identity.encode()).hexdigest()[:32]
                else:
                    base_id = review['web_record']['id']
                    evidence_id = base_id if index == 0 else base_id[:50] + '-' + hashlib.sha256((candidate['citation'] + '\n' + candidate['title']).encode()).hexdigest()[:16]
                evidence = {**candidate, 'id': evidence_id, 'checked_date': datetime.now(kb.TZ).date().isoformat(),
                    'publisher': (record.get('issuer') or '用户提供的本地资料') if local_source else review['source']['publisher'],
                    'source_review_id': review['id'], 'limitations': review['coverage_note']}
                if local_source:
                    evidence.update(source_kind='local_material', url='', sensitivity=record['sensitivity'], model_allowed=record['model_allowed'],
                        material_source={'industry_id': industry, 'material_id': record['id'],
                            'fingerprint': review['source']['fingerprint'], 'attachment_sha256': review['source'].get('attachment_sha256'),
                            'citation': candidate['citation'], 'quote': candidate['quote']})
                else:
                    evidence = {**review['web_record'], **evidence, 'url': review['source']['url'],
                        'checked_date': review['source']['checked_date'], 'source_kind': 'public_web',
                        'related_material': {'industry_id': industry, 'material_id': record['id'],
                            'fingerprint': kb.material_fingerprint(record), 'source_fingerprint': review['source']['fingerprint'],
                            'citation': candidate['citation'], 'quote': candidate['quote']}}
                existing_path = self.store.home(industry) / 'evidence' / (evidence_id + '.json')
                if existing_path.exists():
                    previous = kb.read_json(existing_path)
                    if local_source:
                        previous_source = previous.get('material_source', {})
                        matches = previous.get('source_kind') == 'local_material' and previous_source.get('material_id') == record['id'] and previous_source.get('industry_id') == industry
                    else:
                        matches = previous.get('source_kind', 'public_web') == 'public_web' and previous.get('url') == review['source']['url']
                    if not matches:
                        raise ValueError('证据ID已被其他来源占用，不能覆盖')
                records.append(kb.validate(evidence))
            if len({item['id'] for item in records}) != len(records):
                raise ValueError('本次候选包含重复事实，请合并后确认')
            ids = [item['id'] for item in records]
            result = {**review, 'status': 'approved', 'decision_note': note or review.get('decision_note', ''),
                'decided_at': review.get('decided_at') or kb.now(), 'material_verified': True,
                'extraction_status': 'completed', 'extraction_finished_at': kb.now(),
                'candidates': candidates, 'evidence_ids': ids}
            result.pop('extraction_error', None)
            if not local_source:
                result['material_id'] = record['id']
                result['source'] = {**review['source'], 'approved_material_fingerprint': kb.material_fingerprint(record)}
            home = self.store.home(industry)
            watched = [path, home / 'data/search.sqlite3', home / 'logs/imports.jsonl']
            for evidence in records:
                watched.extend([home / 'evidence' / (evidence['id'] + suffix) for suffix in ('.json', '.md')])
            if record:
                watched.append(self.knowledge.folder(industry) / (record['id'] + '.json'))
                updated = {**record, 'related_evidence_ids': list(dict.fromkeys(record.get('related_evidence_ids', []) + ids))}
                verification = {'status': 'reviewed', 'review_id': review['id'], 'fingerprint': kb.material_fingerprint(record),
                    'reviewed_at': result['decided_at'], 'evidence_ids': ids, 'note': result['decision_note'],
                    'extraction_status': 'completed'}
                if result.get('extraction_method'):
                    verification['extraction_method'] = result['extraction_method']
                if not local_source:
                    verification['public_source_fingerprint'] = review['source']['fingerprint']
                # Evidence existence is validated by _save after the planned imports.
                self.knowledge._save(industry, record, verification=verification, validate_only=True)
            snapshots = {item: item.read_bytes() if item.exists() else None for item in watched}
            history_folders = [self.folder(industry) / 'history' / review['id']]
            history_folders += [home / 'history' / item['id'] for item in records]
            if record:
                history_folders.append(self.knowledge.folder(industry) / 'history' / record['id'])
            histories = {item for folder in history_folders for item in folder.glob('*.json')}
            try:
                if web_material is not None:
                    self.knowledge._save(industry, web_material)
                kb.import_records(home, records, already_locked=True)
                if record:
                    self.knowledge._save(industry, updated, verification=verification)
                self._write(industry, result)
            except Exception:
                self._rollback(snapshots, history_folders, histories)
                raise
            return result
