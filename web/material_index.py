"""Local fixed-size retrieval index. Chunks are source spans, never facts."""
import asyncio
from contextlib import contextmanager
from collections import OrderedDict
import json
import re
import sqlite3
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

    def build(self, industry, material_id, force=False, require_review=True):
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
            if self.approved(record):
                try:
                    await asyncio.to_thread(self.build, record['industry_id'], record['id'])
                except (ValueError, OSError, sqlite3.Error):
                    # Missing/unparseable materials remain visibly unindexed;
                    # never fabricate a completed status for them.
                    continue

    def search(self, industry, query, limit=8, candidate_limit=12, fuzzy=False):
        query_tokens = list(dict.fromkeys(tokens(query)))[:80]
        if not query_tokens:
            return []
        records = self.knowledge.list(industry, '' if fuzzy else query, model_only=True)
        allowed = {(r['industry_id'], r['id']): r for r in records}
        for record in records:
            try:
                self.build(record['industry_id'], record['id'], require_review=False)
            except (ValueError, OSError, sqlite3.Error):
                continue
        hits = []
        match = ' OR '.join('"' + term + '"' for term in query_tokens)
        for entry in self.store.research_entries(industry):
            ids = [key for (home, key) in allowed if home == entry['id']]
            if not ids:
                continue
            with self.connect(entry['id']) as db:
                rows = db.execute('SELECT c.*,d.fingerprint,bm25(chunk_fts) AS rank FROM chunk_fts '
                    'JOIN chunks c ON c.id=chunk_fts.rowid JOIN documents d ON d.material_id=c.material_id '
                    'WHERE chunk_fts MATCH ? AND c.material_id IN (' + ','.join('?' for _ in ids) +
                    ') ORDER BY rank,c.id LIMIT ?', (match, *ids, candidate_limit)).fetchall()
                hits.extend((entry['id'], dict(row)) for row in rows)
        hits.sort(key=lambda item: (item[1]['rank'], item[0], item[1]['id']))
        grouped, parts_cache = OrderedDict(), {}
        for home, hit in hits:
            key = (home, hit['material_id'])
            record = self.knowledge.get(*key)
            if (not record.get('model_allowed') or record.get('sensitivity') == 'restricted'
                    or (industry != 'all' and home != industry and record.get('sensitivity') != 'public')
                    or kb.material_fingerprint(record) != hit['fingerprint']):
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
                'location': hit['location'], 'retrieval_score': -hit['rank']}
            if key not in grouped:
                shown = self.knowledge.display(record)
                grouped[key] = {k: shown.get(k) for k in ('id', 'industry_id', 'title', 'kind_label', 'region',
                    'issuer', 'number', 'version', 'status_label', 'published_date', 'effective_date', 'checked_date',
                    'review_due', 'url', 'related_evidence_ids', 'notes', 'verification_status')}
                grouped[key].update(snippets=[], citation_kind='local-material', source_fingerprint=hit['fingerprint'],
                    retrieval_method='fixed_chunks_fts')
            grouped[key]['snippets'].append(snippet)
            if sum(len(r['snippets']) for r in grouped.values()) >= candidate_limit:
                break
        return list(grouped.values())
