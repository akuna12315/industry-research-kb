"""Download public retrieval weights into this workspace; never download user data."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

MODELS = {
    'embedding': ('Qwen/Qwen3-Embedding-0.6B-GGUF', 'Qwen3-Embedding-0.6B-Q8_0.gguf'),
    'reranker': ('ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF', 'qwen3-reranker-0.6b-q8_0.gguf'),
}


def download(role, folder):
    repo, filename = MODELS[role]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('https://huggingface.co/api/models/' + repo + '?blobs=true', timeout=30) as response:
        metadata = json.load(response)
    info = next(f for f in metadata['siblings'] if f['rfilename'] == filename)
    expected = info['lfs']['sha256']
    target = folder / filename
    digest = hashlib.sha256()
    if target.exists():
        with target.open('rb') as source:
            for part in iter(lambda: source.read(4 * 1024 * 1024), b''):
                digest.update(part)
        if digest.hexdigest() == expected:
            return {'role': role, 'repo': repo, 'revision': metadata['sha'], 'file': filename,
                    'sha256': expected, 'bytes': target.stat().st_size}
    stage = target.with_suffix('.download')
    digest = hashlib.sha256()
    url = f'https://huggingface.co/{repo}/resolve/{metadata["sha"]}/{filename}'
    with opener.open(url, timeout=60) as response, stage.open('wb') as output:
        for part in iter(lambda: response.read(4 * 1024 * 1024), b''):
            digest.update(part)
            output.write(part)
    if digest.hexdigest() != expected or stage.stat().st_size != info['lfs']['size']:
        raise ValueError('Model checksum/size mismatch: ' + filename)
    stage.replace(target)
    return {'role': role, 'repo': repo, 'revision': metadata['sha'], 'file': filename,
            'sha256': expected, 'bytes': target.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parents[1] / 'work/retrieval-models')
    parser.add_argument('--cuda', action='store_true', help='Download the matching official Windows CUDA runtime DLLs')
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda role: download(role, args.directory), MODELS))
    (args.directory / 'manifest.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2), flush=True)
    if args.cuda:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b10767', timeout=30) as response:
            release = json.load(response)
        asset = next(a for a in release['assets'] if a['name'] == 'cudart-llama-bin-win-cuda-13.3-x64.zip')
        archive = args.directory.parent / asset['name']
        if not archive.exists():
            with opener.open(asset['browser_download_url'], timeout=60) as source, archive.with_suffix('.download').open('wb') as output:
                for part in iter(lambda: source.read(4 * 1024 * 1024), b''):
                    output.write(part)
            archive.with_suffix('.download').replace(archive)
        with archive.open('rb') as source:
            digest = 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()
        if archive.stat().st_size != asset['size'] or asset.get('digest') != digest:
            raise ValueError('CUDA archive checksum/size mismatch')
        target = args.directory.parent / 'retrieval-cuda'
        target.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as source:
            for item in source.infolist():
                path = (target / item.filename).resolve()
                if not path.is_relative_to(target.resolve()) or path.suffix.lower() != '.dll':
                    if item.is_dir():
                        continue
                    raise ValueError('Unexpected CUDA archive member')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source.read(item))
        print(json.dumps({'cuda_runtime': str(target), 'archive_digest': digest}), flush=True)


if __name__ == '__main__':
    main()
