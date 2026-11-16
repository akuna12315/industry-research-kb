"""Build/status/evaluate local retrieval without using the answer model or changing materials."""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.knowledge import Knowledge
from web.material_index import MaterialIndex
from web.retrieval import LocalModels, settings
from web.service import Store, kb


def evaluate(store, cases, modes):
    from web.agent_runtime import AgentRuntime
    index = MaterialIndex(store)
    runtime = AgentRuntime(store)
    details = []
    for case in cases:
        if not case.get('relevant'):
            raise ValueError('Each case needs explicitly labelled relevant source quotes')
        for gold in case['relevant']:
            home = gold.get('industry_id', case['industry'])
            if home == 'all':
                raise ValueError('Cross-industry gold labels require industry_id')
            record = Knowledge(store).get(home, gold['material_id'])
            bodies = [p['text'] for _, p in Knowledge(store).index_parts(record)] if record.get('extraction', {}).get('text_index') else [record.get('content') or record.get('summary', '')]
            if not gold.get('quote') or not any(gold['quote'] in body for body in bodies):
                raise ValueError('Gold quote is not verbatim in the current source: ' + case['id'])
        for mode in modes:
            started = time.monotonic()
            method = 'hybrid' if mode == 'hybrid_rerank' else mode
            records = index.search(case['industry'], case['query'], limit=100,
                candidate_limit=12 if mode == 'lexical_12' else 48, fuzzy=True,
                retrieval_mode='lexical' if mode == 'lexical_12' else method)
            retrieval_method = records[0]['retrieval_method'] if records else 'no_hits'
            if mode == 'hybrid_rerank':
                records = asyncio.run(runtime.rerank_materials(case['query'], records, industry=case['industry']))
            pool = [(r, s) for r in records for s in r['snippets']]
            pool.sort(key=lambda pair: pair[1].get('rerank_rank', 1000) if mode == 'hybrid_rerank'
                      else -pair[1].get('retrieval_score', 0))
            found = []
            for record, snippet in pool:
                found.append({n for n, gold in enumerate(case['relevant'])
                              if record['industry_id'] == gold.get('industry_id', case['industry'])
                              and record['id'] == gold['material_id'] and gold['quote'] in snippet['text']})
            relevant_count = len(case['relevant'])
            union = lambda k: set().union(*found[:k]) if found[:k] else set()
            first = next((n for n, match in enumerate(found, 1) if match), None)
            relevant_materials = {(gold.get('industry_id', case['industry']), gold['material_id']) for gold in case['relevant']}
            ideal_count = min(6, len(relevant_materials))
            # Duplicate overlapping hits must not receive repeated relevance credit.
            seen = set()
            dcg = 0.0
            for rank, (matches, pair) in enumerate(zip(found[:6], pool[:6]), 1):
                material = pair[0]['industry_id'], pair[0]['id']
                if matches and material not in seen:
                    dcg += 1 / math.log2(rank + 1)
                    seen.add(material)
            ideal = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
            details.append({'id': case['id'], 'mode': mode, 'query': case['query'],
                'recall_at_6': len(union(6)) / relevant_count, 'recall_at_48': len(union(48)) / relevant_count,
                'mrr': 1 / first if first else 0, 'ndcg_at_6': dcg / ideal if ideal else 0,
                'seconds': round(time.monotonic() - started, 3), 'retrieval_method': retrieval_method,
                'ranking_method': records[0].get('ranking_method') if records else None,
                'hits': [{'material_id': r['id'], 'citation': s['citation']} for r, s in pool[:6]]})
            print(json.dumps({key: details[-1][key] for key in ('id', 'mode', 'recall_at_6', 'seconds', 'retrieval_method', 'ranking_method')}), flush=True)
    summary = {}
    for mode in modes:
        group = [r for r in details if r['mode'] == mode]
        summary[mode] = {key: round(sum(r[key] for r in group) / len(group), 4)
                         for key in ('recall_at_6', 'recall_at_48', 'mrr', 'ndcg_at_6', 'seconds')}
        summary[mode]['fallbacks'] = sum('fallback' in r['retrieval_method'] or r['ranking_method'] == 'local_rank_fallback' for r in group)
    return {'as_of': kb.now(), 'cases': len(cases), 'summary': summary, 'details': details,
            'limits': 'Labelled source-span retrieval only, not answer accuracy. Fixed chunks held constant. No answer-model calls. Tiny selected cases are not general industry coverage.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'status', 'evaluate'))
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--industry', required=True)
    parser.add_argument('--cases', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    store = Store(args.root)
    index = MaterialIndex(store)
    records = [r for r in Knowledge(store).list(args.industry) if r.get('model_allowed') and r.get('sensitivity') != 'restricted']
    if args.action == 'build':
        backend = LocalModels(store)
        for record in records:
            started = time.monotonic()
            index.build(record['industry_id'], record['id'])
            index.ensure_vectors([record], backend)
            print(json.dumps({'material_id': record['id'], 'seconds': round(time.monotonic()-started, 3)}), flush=True)
    elif args.action == 'evaluate':
        if not args.cases or not args.output:
            parser.error('evaluate requires --cases and --output')
        cases = json.loads(args.cases.read_text(encoding='utf-8-sig'))
        if args.industry != 'all' and any(case['industry'] != args.industry for case in cases):
            parser.error('cases are outside --industry')
        report = evaluate(store, cases, ('lexical_12', 'lexical', 'dense', 'hybrid', 'hybrid_rerank'))
        report['cases_sha256'] = hashlib.sha256(args.cases.read_bytes()).hexdigest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        encoder = LocalModels(store).encoder
        counts = []
        for entry in store.research_entries(args.industry):
            if not index.path(entry['id']).exists():
                continue
            with index.connect(entry['id']) as db:
                count = db.execute('SELECT count(*) FROM chunk_vectors WHERE encoder=?', (encoder,)).fetchone()[0]
            counts.append({'industry': entry['id'], 'vectors_for_encoder': count})
        print(json.dumps(counts))


if __name__ == '__main__':
    main()
