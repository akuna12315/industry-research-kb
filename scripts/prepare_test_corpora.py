"""Prepare bounded public retrieval corpora in an isolated test directory."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / 'work/test-corpora/small-company-v1'


def get_json(url):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=45) as response:
        return json.load(response)


def digest(path, algorithm='sha256'):
    with path.open('rb') as source:
        return hashlib.file_digest(source, algorithm).hexdigest()


def fetch(spec):
    target = Path(spec['target'])
    target.parent.mkdir(parents=True, exist_ok=True)
    def valid(path):
        if not path.exists() or spec.get('bytes') is not None and path.stat().st_size != spec['bytes']:
            return False
        if spec.get('sha256'):
            return digest(path) == spec['sha256']
        if spec.get('git_blob'):
            value = path.read_bytes()
            return hashlib.sha1(b'blob ' + str(len(value)).encode() + b'\0' + value).hexdigest() == spec['git_blob']
        return False
    if not valid(target):
        for attempt in range(3):
            try:
                stage = target.with_name(target.name + '.part')
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(spec['url'], timeout=90) as response, stage.open('wb') as output:
                    for part in iter(lambda: response.read(4 * 1024 * 1024), b''):
                        output.write(part)
                if not valid(stage):
                    raise ValueError('Download checksum/size mismatch: ' + target.name)
                stage.replace(target)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1)
    result = {**spec, 'sha256': digest(target), 'bytes': target.stat().st_size}
    print(json.dumps({'downloaded': target.name, 'bytes': result['bytes']}), flush=True)
    return result


def download(folder):
    raw = folder / 'raw'
    specs, catalogs = [], {}
    def hf(name, repo, predicate):
        metadata = get_json('https://huggingface.co/api/datasets/' + repo + '?blobs=true')
        catalogs[name] = {'repository': repo, 'revision': metadata['sha'],
                          'card': metadata.get('cardData', {}), 'files': metadata['siblings']}
        for item in metadata['siblings']:
            filename = item['rfilename']
            if not predicate(filename):
                continue
            spec = {'dataset': name, 'url': f'https://huggingface.co/datasets/{repo}/resolve/{metadata["sha"]}/{filename}',
                    'target': str(raw / name / filename), 'bytes': item.get('size')}
            if item.get('lfs'):
                spec['sha256'] = item['lfs']['sha256']
            else:
                spec['git_blob'] = item['blobId']
            specs.append(spec)
    hf('t2', 'C-MTEB/T2Retrieval', lambda f: f.endswith('.parquet') or f == 'README.md')
    hf('t2-qrels', 'C-MTEB/T2Retrieval-qrels', lambda f: f.endswith('.parquet') or f == 'README.md')
    # dev includes full positive and sampled negative documents; do not download
    # the 1.38 GB Chinese collection or the training corpus.
    hf('mldr-zh', 'Shitao/MLDR', lambda f: f in ('README.md', 'mldr-v1.0-zh/dev.jsonl.gz', 'qrels/qrels.mldr-v1.0-zh-dev.tsv'))
    release = get_json('https://api.github.com/repos/onyx-dot-app/EnterpriseRAG-Bench/releases/tags/v1.0.0')
    catalogs['enterprise'] = {'repository': 'onyx-dot-app/EnterpriseRAG-Bench', 'release': release['tag_name'],
                              'assets': release['assets'], 'license': 'MIT', 'language': 'en', 'synthetic': True}
    for asset in release['assets']:
        name = asset['name']
        if name.startswith(('confluence_slice_', 'google_drive_slice_')) or name == 'questions.jsonl':
            if not asset.get('digest', '').startswith('sha256:'):
                raise ValueError('Release asset has no published SHA-256')
            specs.append({'dataset': 'enterprise', 'url': asset['browser_download_url'],
                          'target': str(raw / 'enterprise' / name), 'bytes': asset['size'],
                          'sha256': asset['digest'].split(':', 1)[1]})
    # A single isolated, verified wheel, never installed in the application venv.
    package = get_json('https://pypi.org/pypi/pyarrow/22.0.0/json')
    wheel = next(x for x in package['urls'] if 'cp314-cp314-win_amd64' in x['filename'])
    specs.append({'dataset': 'tools', 'url': wheel['url'], 'target': str(folder / 'tools' / wheel['filename']),
                  'bytes': wheel['size'], 'sha256': wheel['digests']['sha256']})
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'source-catalog.json').write_text(json.dumps(catalogs, ensure_ascii=False, indent=2), encoding='utf-8')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, specs))
    (folder / 'downloads.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return results


def load_arrow(folder):
    target = folder / 'tools/pyarrow-runtime'
    if not (target / 'pyarrow/__init__.py').is_file():
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(next((folder / 'tools').glob('pyarrow*.whl'))) as archive:
            for item in archive.infolist():
                destination = (target / item.filename).resolve()
                if not destination.is_relative_to(target.resolve()):
                    raise ValueError('Unsafe wheel member')
                if item.is_dir():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(item))
    sys.path.insert(0, str(target))
    import pyarrow.parquet as parquet
    return parquet


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(path.name + '.part')
    count = 0
    with stage.open('w', encoding='utf-8', newline='\n') as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
            count += 1
    stage.replace(path)
    return count


def prepare_main(folder):
    """Retain the complete downloaded Chinese collection, not a toy document pool."""
    parquet = load_arrow(folder)
    target = folder / 'prepared/t2-full'
    corpus_file = next((folder / 'raw/t2/data').glob('corpus*.parquet'))
    query_file = next((folder / 'raw/t2/data').glob('queries*.parquet'))
    qrels_file = next((folder / 'raw/t2-qrels/data').glob('*.parquet'))
    documents, characters = set(), 0
    def corpus():
        nonlocal characters
        for batch in parquet.ParquetFile(corpus_file).iter_batches(batch_size=1024):
            for row in batch.to_pylist():
                key, text = str(row['id']), row['text']
                if key in documents or not isinstance(text, str):
                    raise ValueError('Duplicate document ID or invalid text')
                documents.add(key)
                characters += len(text)
                yield {'_id': key, 'title': '', 'text': text}
    document_count = write_jsonl(target / 'corpus.jsonl', corpus())
    queries = parquet.read_table(query_file).to_pylist()
    query_ids = {str(row['id']) for row in queries}
    if len(query_ids) != len(queries):
        raise ValueError('Duplicate query ID')
    query_count = write_jsonl(target / 'queries.jsonl', ({'_id': str(row['id']), 'text': row['text']} for row in queries))
    qrels = parquet.read_table(qrels_file).to_pylist()
    bad = [row for row in qrels if str(row['qid']) not in query_ids or str(row['pid']) not in documents]
    if bad:
        raise ValueError('Relevance labels refer to missing queries or documents: ' + str(len(bad)))
    grades = {}
    positives = set()
    for row in qrels:
        grades[str(row['score'])] = grades.get(str(row['score']), 0) + 1
        if row['score'] > 0:
            positives.add(str(row['qid']))
    # BEIR-compatible JSONL + qrels TSV. Original query, document and grade IDs retained.
    stage = target / 'qrels/dev.tsv.part'
    stage.parent.mkdir(parents=True, exist_ok=True)
    with stage.open('w', encoding='utf-8', newline='\n') as output:
        output.write('query-id\tcorpus-id\tscore\n')
        for row in qrels:
            output.write(f"{row['qid']}\t{row['pid']}\t{row['score']}\n")
    stage.replace(target / 'qrels/dev.tsv')
    # Local plan only: official labels are dev, not an official hidden test split.
    ordered = sorted(positives, key=lambda key: hashlib.sha256(('small-company-v1:' + key).encode()).digest())
    plan = {'seed': 'small-company-v1', 'corpus': 'complete downloaded T2 corpus',
            'development_query_ids': ordered[:200], 'held_out_query_ids': ordered[200:1000],
            'smoke_query_ids': ordered[:50],
            'note': 'Local fixed query split of public dev data, not an official test set or guaranteed unseen model data. No training or evaluation has run.'}
    (target / 'evaluation-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    files = [{"file": p.relative_to(target).as_posix(), "bytes": p.stat().st_size, "sha256": digest(p)}
             for p in sorted(target.rglob('*')) if p.is_file() and p.name != 'manifest.json']
    summary = {'dataset': 'C-MTEB/T2Retrieval', 'qrels_dataset': 'C-MTEB/T2Retrieval-qrels',
               'scope': 'Complete downloaded corpus, queries and dev qrels; no text filtering or truncation',
               'documents': document_count, 'queries': query_count, 'qrels': len(qrels),
               'source_characters': characters, 'grade_counts': grades,
               'queries_with_positive_labels': len(positives), 'missing_label_references': len(bad),
               'evaluation_run': False, 'files': files}
    (target / 'manifest.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in summary.items() if key != 'files'}, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--download-only', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if not args.prepare_only:
        download(args.output.resolve())
    if not args.download_only:
        prepare_main(args.output.resolve())


if __name__ == '__main__':
    main()
