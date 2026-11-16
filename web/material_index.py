"""Local fixed-size retrieval index. Chunks are source spans, never facts."""
import asyncio
from contextlib import contextmanager
from collections import OrderedDict
import json
import math
import re
import sqlite3
import struct
import unicodedata

from .service import kb

CHUNK_SIZE = 1200
OVERLAP = 180
VERSION = 'fixed-chunks-v1'


def tokens(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    result = []
    for word in re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+', text):
        if '\u3400' <= word[0] <= '\u9fff':
            result.extend(word if len(word) == 1 else (word[n:n+2] for n in range(len(word)-1)))
        else:
            result.append(word)
    return result


class MaterialIndex:
    def __init__(self, store):
        from .knowledge import Knowledge
        self.store = store
        self.knowledge = Knowledge(store)

    def path(self, industry):
        return self.store.home(industry) / 'data' / 'material-chunks.sqlite3'

    @contextmanager
    def connect(self, industry):
        path = self.path(industry)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=15)
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE IF NOT EXISTS documents(material_id TEXT PRIMARY KEY, fingerprint TEXT, summary TEXT);
            CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, material_id TEXT, block INTEGER,
                start INTEGER, stop INTEGER, location TEXT, terms TEXT);
            CREATE INDEX IF NOT EXISTS chunks_material ON chunks(material_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(terms, content='chunks', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS chunks_insert AFTER INSERT ON chunks BEGIN
                INSERT INTO chunk_fts(rowid,terms) VALUES(new.id,new.terms); END;
            CREATE TRIGGER IF NOT EXISTS chunks_delete AFTER DELETE ON chunks BEGIN
                INSERT INTO chunk_fts(chunk_fts,rowid,terms) VALUES('delete',old.id,old.terms); END;
            CREATE TABLE IF NOT EXISTS chunk_vectors(chunk_id INTEGER PRIMARY KEY, encoder TEXT,
                fingerprint TEXT, dimensions INTEGER, vector BLOB);
            CREATE TABLE IF NOT EXISTS vector_documents(material_id TEXT PRIMARY KEY, encoder TEXT,
                fingerprint TEXT, chunks INTEGER);
            CREATE TRIGGER IF NOT EXISTS vectors_delete AFTER DELETE ON chunks BEGIN
                DELETE FROM chunk_vectors WHERE chunk_id=old.id;
                DELETE FROM vector_documents WHERE material_id=old.material_id; END;
        ''')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def approved(record):
        verification = record.get('verification', {})
        return verification.get('status') == 'reviewed' and verification.get('fingerprint') == kb.material_fingerprint(record)

    def latest(self, industry, material_id):
        if not self.path(industry).exists():
            return None
        with self.connect(industry) as db:
            row = db.execute('SELECT fingerprint,summary FROM documents WHERE material_id=?', (material_id,)).fetchone()
        if not row:
            return None
        record = self.knowledge.get(industry, material_id)
        result = json.loads(row['summary'])
        if row['fingerprint'] != kb.material_fingerprint(record):
            result.update(status='invalidated', error='原文或资料版本已变化，请重建全文索引')
        return result

    def build(self, industry, material_id, force=False, require_review=False):
        record = self.knowledge.get(industry, material_id)
        if require_review and not self.approved(record):
            raise ValueError('请先通过当前资料版本的核验')
        previous = self.latest(industry, material_id)
        if not force and previous and previous['status'] == 'completed':
            return previous
        fingerprint = kb.material_fingerprint(record)
        parts = list(self.knowledge.index_parts(record)) if record.get('extraction', {}).get('text_index') else [
            (None, {'text': record.get('content') or record.get('summary', ''), 'first_location': '正文', 'last_location': '正文'})]
        rows, characters = [], 0
        for number, part in parts:
            body = part['text']
            characters += len(body)
            start, covered = 0, 0
            while start < len(body):
                stop = min(len(body), start + CHUNK_SIZE)
                assert start <= covered and stop > covered
                location = part.get('first_location', '正文') + ' 至 ' + part.get('last_location', '正文')
                markers = re.findall(r'【([^】\n]+)】', body[:start + 1])
                if markers:
                    location = markers[-1] + '起；' + location
                rows.append((material_id, number + 1 if number is not None else 0, start, stop,
                    location, ' '.join(tokens(body[start:stop]) + tokens(record['title']))))
                covered = stop
                if stop == len(body):
                    break
                start = stop - OVERLAP
            assert covered == len(body)
        if not rows:
            raise ValueError('资料没有可解析文字；扫描图片需先OCR，不能标记全文已索引')
        extraction = record.get('extraction', {})
        tables, images = extraction.get('tables', 0), extraction.get('images', 0)
        document_processing=extraction.get('document_processing')
        if document_processing:
            tables=document_processing.get('tables',tables)
            image_coverage=document_processing.get('coverage',{})
            images=image_coverage.get('unprocessed_images',images)
        result = {'id': 'index-' + material_id, 'industry_id': industry, 'material_id': material_id,
            'title': record['title'], 'mode': 'hard_chunk_index', 'version': VERSION, 'status': 'completed',
            'fingerprint': fingerprint, 'created_at': (previous or {}).get('created_at', kb.now()),
            'updated_at': kb.now(), 'chunk_size': CHUNK_SIZE, 'overlap': OVERLAP,
            'source_characters': extraction.get('characters', characters), 'indexed_chunks': len(rows),
            'coverage_note': '全文可解析文字和原生表格按固定长度切分；保留逐字定位。图片未OCR，扫描件及图片文字不在覆盖内。分块是原文索引，不是经核实的事实证据。',
            'progress': {'completed': len(rows), 'total': len(rows), 'failed': 0,
                'processed_characters': characters, 'total_characters': characters,
                'processed_tables': tables, 'total_tables': tables, 'unprocessed_images': images,
                'evidence_count': 0}, 'usage': {'requests': 0}, 'evidence_ids': []}
        if document_processing:
            result['document_processing']=document_processing
            result['coverage_note']='索引包含原生文字及明确标注的结构化表格行/OCR/视觉识别表示。后者不是原文连续段落或事实核验。图表实际页数、图片识别范围及未完成项见图表处理报告。'
        # The index stores terms and locators only; all returned text is read
        # from the authoritative material/index parts. Source consent is live.
        with kb.lock(self.store.root):
            current = self.knowledge.get(industry, material_id)
            if kb.material_fingerprint(current) != fingerprint or (require_review and not self.approved(current)):
                raise ValueError('资料版本或核验状态已变化，未替换索引')
            with self.connect(industry) as db:
                db.execute('DELETE FROM chunks WHERE material_id=?', (material_id,))
                db.executemany('INSERT INTO chunks(material_id,block,start,stop,location,terms) VALUES(?,?,?,?,?,?)', rows)
                db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?)',
                    (material_id, fingerprint, kb.json_text(result)))
        return result

    async def restore(self):
        for record in await asyncio.to_thread(self.knowledge.list, 'all'):
            if record.get('model_allowed') and record.get('sensitivity') != 'restricted':
                try:
                    await asyncio.to_thread(self.build, record['industry_id'], record['id'])
                except (ValueError, OSError, sqlite3.Error):
                    # Missing/unparseable materials remain visibly unindexed;
                    # never fabricate a completed status for them.
                    continue

    def ensure_vectors(self, records, backend):
        """Incrementally encode locally, outside the write lock; commit whole documents."""
        encoder = backend.encoder
        for record in records:
            home, material_id = record['industry_id'], record['id']
            fingerprint = kb.material_fingerprint(record)
            with self.connect(home) as db:
                ready = db.execute('SELECT * FROM vector_documents WHERE material_id=?', (material_id,)).fetchone()
                rows = [dict(row) for row in db.execute('SELECT * FROM chunks WHERE material_id=? ORDER BY id', (material_id,))]
                count = db.execute('SELECT count(*) FROM chunk_vectors v JOIN chunks c ON c.id=v.chunk_id '
                    'WHERE c.material_id=? AND v.encoder=? AND v.fingerprint=?',
                    (material_id, encoder, fingerprint)).fetchone()[0]
            if ready and ready['encoder'] == encoder and ready['fingerprint'] == fingerprint and count == len(rows) == ready['chunks']:
                continue
            if not rows:
                continue
            bodies = {n + 1: p['text'] for n, p in self.knowledge.index_parts(record)} if rows[0]['block'] else {
                0: record.get('content') or record.get('summary', '')}
            vectors = []
            for offset in range(0, len(rows), 4):
                current = self.knowledge.get(home, material_id)
                if (not current.get('model_allowed') or current.get('sensitivity') == 'restricted'
                        or kb.material_fingerprint(current) != fingerprint):
                    raise ValueError('编码期间资料权限或版本已变化')
                batch = rows[offset:offset + 4]
                vectors.extend(backend.embed([record['title'] + '\n' + bodies[row['block']][row['start']:row['stop']]
                                              for row in batch]))
            if len(vectors) != len(rows):
                raise ValueError('编码数量与原文块不匹配')
            with kb.lock(self.store.root):
                current = self.knowledge.get(home, material_id)
                if (not current.get('model_allowed') or current.get('sensitivity') == 'restricted'
                        or kb.material_fingerprint(current) != fingerprint):
                    raise ValueError('编码期间资料权限或版本已变化')
                with self.connect(home) as db:
                    current_rows = [dict(row) for row in db.execute('SELECT * FROM chunks WHERE material_id=? ORDER BY id', (material_id,))]
                    if rows != current_rows:
                        raise ValueError('编码期间原文索引已变化')
                    db.executemany('INSERT OR REPLACE INTO chunk_vectors VALUES(?,?,?,?,?)', [
                        (row['id'], encoder, fingerprint, len(vector), struct.pack('<' + 'f' * len(vector), *vector))
                        for row, vector in zip(rows, vectors)])
                    db.execute('INSERT OR REPLACE INTO vector_documents VALUES(?,?,?,?)',
                               (material_id, encoder, fingerprint, len(rows)))

    def dense_hits(self, allowed, backend, query, limit):
        from .retrieval import normalized_vector
        vector = normalized_vector(backend.embed([query], query=True)[0])
        hits = []
        for home in sorted({home for home, _ in allowed}):
            ids = [key for h, key in allowed if h == home]
            with self.connect(home) as db:
                rows = db.execute('SELECT c.*, d.fingerprint,v.vector,v.dimensions FROM chunk_vectors v '
                    'JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.material_id=c.material_id '
                    'WHERE v.encoder=? AND v.fingerprint=d.fingerprint AND c.material_id IN (' +
                    ','.join('?' for _ in ids) + ')', (backend.encoder, *ids)).fetchall()
            for raw in rows:
                row = dict(raw)
                if row['dimensions'] != len(vector) or len(row['vector']) != len(vector) * 4:
                    raise ValueError('已存向量维度无效，请重建索引')
                stored = struct.unpack('<' + 'f' * len(vector), row.pop('vector'))
                score = sum(a * b for a, b in zip(stored, vector))
                if not math.isfinite(score):
                    raise ValueError('已存向量无效，请重建索引')
                row.update(rank=-score, dense_score=score)
                hits.append((home, row))
        return sorted(hits, key=lambda hit: (hit[1]['rank'], hit[0], hit[1]['id']))[:limit]

    def expand(self, records, maximum=2200):
        """Expand selected spans in their original block; never synthesize source text."""
        result = []
        for record in records:
            try:
                current = self.knowledge.get(record['industry_id'], record['id'])
            except (ValueError, OSError):
                continue
            if (not current.get('model_allowed') or current.get('sensitivity') == 'restricted'
                    or kb.material_fingerprint(current) != record['source_fingerprint']):
                continue
            bodies = {n + 1: p['text'] for n, p in self.knowledge.index_parts(current)} if current.get('extraction', {}).get('text_index') else {
                0: current.get('content') or current.get('summary', '')}
            snippets = []
            for snippet in record['snippets']:
                match = re.fullmatch(re.escape(current['id']) + r'#(?:block-(\d+)-)?chars-(\d+)-(\d+)', snippet['citation'])
                if not match:
                    continue
                block, start, stop = int(match[1] or 0), int(match[2]) - 1, int(match[3])
                body = bodies.get(block, '')
                if body[start:stop] != snippet['text']:
                    continue
                extra = max(0, maximum - (stop - start))
                left = max(0, start - extra // 2)
                right = min(len(body), left + maximum)
                left = max(0, right - maximum)
                prefix = f"{current['id']}#block-{block}-chars-" if block else f"{current['id']}#chars-"
                snippets.append({**snippet, 'citation': prefix + f'{left + 1}-{right}', 'text': body[left:right],
                                 'matched_citation': snippet['citation']})
            if snippets:
                result.append({**record, 'snippets': snippets})
        return result

    def search(self, industry, query, limit=8, candidate_limit=None, fuzzy=False, model_only=True, kind='', material_scope=None,
               retrieval_mode=None):
        from .retrieval import LocalModels, settings, rrf
        cfg = settings(self.store.root)
        mode = retrieval_mode or (cfg['mode'] if cfg['enabled'] else 'lexical')
        if mode not in ('lexical', 'dense', 'hybrid'):
            raise ValueError('检索模式无效')
        candidate_limit = candidate_limit if candidate_limit is not None else cfg['candidate_limit'] if cfg['enabled'] else 12
        candidate_limit = max(1, min(120, candidate_limit))
        query_tokens = list(dict.fromkeys(tokens(query)))[:80]
        if not query.strip() or not query_tokens and mode == 'lexical':
            return []
        records = self.knowledge.list(industry, '' if fuzzy or mode != 'lexical' else query, kind=kind, model_only=model_only)
        if material_scope:
            selected = {(r['industry_id'], r['id']) for r in material_scope}
            records = [r for r in records if (r['industry_id'], r['id']) in selected]
        records = [record for record in records if record.get('model_allowed') and record.get('sensitivity') != 'restricted']
        allowed = {(r['industry_id'], r['id']): r for r in records}
        for record in records:
            try:
                self.build(record['industry_id'], record['id'])
            except (ValueError, OSError, sqlite3.Error):
                continue
        hits, branches = [], []
        match = ' OR '.join('"' + term + '"' for term in query_tokens)
        for entry in self.store.research_entries(industry):
            ids = [key for (home, key) in allowed if home == entry['id']]
            if not ids:
                continue
            if not query_tokens:
                continue
            with self.connect(entry['id']) as db:
                rows = db.execute('SELECT c.*,d.fingerprint,bm25(chunk_fts) AS rank FROM chunk_fts '
                    'JOIN chunks c ON c.id=chunk_fts.rowid JOIN documents d ON d.material_id=c.material_id '
                    'WHERE chunk_fts MATCH ? AND c.material_id IN (' + ','.join('?' for _ in ids) +
                    ') ORDER BY rank,c.id LIMIT ?', (match, *ids, max(candidate_limit, cfg['branch_limit']))).fetchall()
                branch = [(entry['id'], dict(row)) for row in rows]
                branches.append(branch)
                hits.extend(branch)
        hits.sort(key=lambda item: (item[1]['rank'], item[0], item[1]['id']))
        method, note = 'fixed_chunks_fts', ''
        if mode != 'lexical':
            # Per-library BM25 statistics differ; use ranks across libraries too.
            lexical = rrf(branches, cfg['rrf_k'])[:cfg['branch_limit']]
            try:
                backend = LocalModels(self.store, cfg)
                self.ensure_vectors(records, backend)
                dense = self.dense_hits(allowed, backend, query, cfg['branch_limit'])
                hits = rrf([lexical, dense], cfg['rrf_k']) if mode == 'hybrid' else dense
                method = 'hybrid_rrf' if mode == 'hybrid' else 'dense_vector'
            except Exception as exc:
                hits = lexical
                method, note = 'lexical_fallback', '本机语义检索不可用，使用关键词结果（' + type(exc).__name__ + '）；未声称完成向量检索。'
        grouped, parts_cache = OrderedDict(), {}
        for home, hit in hits:
            key = (home, hit['material_id'])
            record = self.knowledge.get(*key)
            if (kb.material_fingerprint(record) != hit['fingerprint']
                    or (model_only and (not record.get('model_allowed') or record.get('sensitivity') == 'restricted'
                        or (industry != 'all' and home != industry and record.get('sensitivity') != 'public')))
                    or not record.get('model_allowed') or record.get('sensitivity') == 'restricted'):
                continue
            if key not in grouped and len(grouped) >= limit:
                continue
            if key not in parts_cache:
                parts_cache[key] = {n + 1: p['text'] for n, p in self.knowledge.index_parts(record)} if hit['block'] else {
                    0: record.get('content') or record.get('summary', '')}
            body = parts_cache[key].get(hit['block'], '')
            start, stop = hit['start'], hit['stop']
            if not 0 <= start < stop <= len(body):
                continue
            prefix = f"{record['id']}#block-{hit['block']}-chars-" if hit['block'] else f"{record['id']}#chars-"
            snippet = {'citation': prefix + f'{start+1}-{stop}', 'text': body[start:stop],
                'location': hit['location'], 'retrieval_score': hit.get('fusion_score', -hit['rank'])}
            if key not in grouped:
                shown = self.knowledge.display(record)
                grouped[key] = {k: shown.get(k) for k in ('id', 'industry_id', 'title', 'kind_label', 'region',
                    'issuer', 'number', 'version', 'status_label', 'published_date', 'effective_date', 'checked_date',
                    'review_due', 'url', 'related_evidence_ids', 'notes', 'verification_status')}
                grouped[key].update(snippets=[], citation_kind='local-material', source_fingerprint=hit['fingerprint'],
                    retrieval_method=method, retrieval_note=note)
            grouped[key]['snippets'].append(snippet)
            if sum(len(r['snippets']) for r in grouped.values()) >= candidate_limit:
                break
        return list(grouped.values())
