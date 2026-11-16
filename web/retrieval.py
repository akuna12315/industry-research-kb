"""Local embedding/reranking clients. No keys, proxies, remote calls or auto-downloads."""
import atexit
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlsplit

import httpx

from .service import kb

DEFAULTS = {'enabled': False, 'mode': 'hybrid', 'candidate_limit': 48, 'branch_limit': 40,
            'final_limit': 6, 'rrf_k': 60, 'context_chars': 2200,
            'query_instruction': 'Given a question, retrieve passages that directly answer it.',
            'embedding': {}, 'reranker': {}, 'runtime': {}}
_processes = {}
_process_lock = threading.RLock()


class RetrievalUnavailable(ValueError):
    pass


def settings(root):
    path = Path(root) / 'config/retrieval.json'
    result = {**DEFAULTS, **(kb.read_json(path) if path.exists() else {})}
    if type(result['enabled']) is not bool or result['mode'] not in ('lexical', 'dense', 'hybrid'):
        raise RetrievalUnavailable('检索配置的模式无效')
    for key, low, high in (('candidate_limit', 6, 120), ('branch_limit', 6, 120),
                           ('final_limit', 1, 12), ('rrf_k', 1, 1000), ('context_chars', 1200, 4000)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise RetrievalUnavailable('检索配置超出范围：' + key)
    return result


def local_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1')
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/') or not parsed.port):
        raise RetrievalUnavailable('检索模型仅允许显式端口的本机回环HTTP地址')
    return value.rstrip('/')


def normalized_vector(values, dimension=None):
    if not isinstance(values, list) or not values or len(values) > 8192:
        raise RetrievalUnavailable('编码模型未返回有效向量')
    if dimension is not None and len(values) != dimension:
        raise RetrievalUnavailable('向量维度不匹配')
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise RetrievalUnavailable('向量包含无效数值')
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise RetrievalUnavailable('向量长度无效')
    return [float(value / norm) for value in values]


def rrf(branches, k=60):
    """Fuse rankings, never incomparable BM25/cosine scores."""
    scores, items = {}, {}
    for branch in branches:
        seen = set()
        for rank, (home, row) in enumerate(branch, 1):
            key = home, row['id']
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            items.setdefault(key, (home, row))
    return [(items[key][0], {**items[key][1], 'fusion_score': scores[key]})
            for key in sorted(scores, key=lambda key: (-scores[key], key))]


def close_models(root=None):
    with _process_lock:
        for key, process in list(_processes.items()):
            if root is not None and key[0] != str(Path(root).resolve()):
                continue
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            del _processes[key]


atexit.register(close_models)


class LocalModels:
    def __init__(self, store, cfg=None):
        self.store = store
        self.cfg = cfg or settings(store.root)

    @property
    def encoder(self):
        spec = self.cfg['embedding']
        if not spec.get('model') or not spec.get('revision'):
            raise RetrievalUnavailable('编码模型及版本尚未配置')
        payload = {key: spec.get(key) for key in ('model', 'revision', 'dimension')}
        payload['instruction'] = self.cfg['query_instruction']
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _identity(self, client, base, role):
        response = client.get(base + '/v1/models')
        response.raise_for_status()
        if self.cfg[role]['model'] not in [m.get('id') for m in response.json().get('data', [])]:
            raise RetrievalUnavailable('本机端口运行的检索模型与配置不一致')

    def ensure(self, role):
        spec = self.cfg[role]
        base = local_url(spec.get('url', ''))
        with _process_lock:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=2) as client:
                try:
                    self._identity(client, base, role)
                    return base
                except RetrievalUnavailable:
                    raise
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
            runtime = self.cfg['runtime']
            if not runtime.get('auto_start'):
                raise RetrievalUnavailable('本机检索模型尚未启动')
            binary = Path(runtime.get('binary', ''))
            weight = Path(spec.get('file', ''))
            if not weight.is_absolute():
                weight = self.store.root / weight
            if not binary.is_file() or not weight.is_file():
                raise RetrievalUnavailable('本机检索运行程序或模型文件缺失')
            if len(spec.get('revision', '')) == 64:
                with weight.open('rb') as source:
                    if hashlib.file_digest(source, 'sha256').hexdigest() != spec['revision']:
                        raise RetrievalUnavailable('本机检索模型文件校验失败')
            key = str(self.store.root.resolve()), role
            old = _processes.get(key)
            if old and old.poll() is None:
                raise RetrievalUnavailable('检索模型已启动但尚未就绪或端口冲突')
            args = [str(binary), '-m', str(weight), '--alias', spec['model'], '--host', '127.0.0.1',
                    '--port', str(urlsplit(base).port), '-ngl', str(runtime.get('gpu_layers', 99)),
                    '-c', '4096', '-b', '2048', '-ub', '2048', '-np', '1', '--log-disable',
                    '--pooling', 'last' if role == 'embedding' else 'rank',
                    '--embeddings' if role == 'embedding' else '--reranking']
            # No shell, user-provided command strings, visible windows or credentials.
            environment = dict(os.environ)
            if runtime.get('gpu_layers') == 0:
                # CPU retrieval must not reserve a CUDA context beside an 8 GB QA model.
                environment['CUDA_VISIBLE_DEVICES'] = '-1'
            if runtime.get('library_directory'):
                libraries = Path(runtime['library_directory'])
                if not libraries.is_absolute():
                    libraries = self.store.root / libraries
                environment['PATH'] = str(libraries) + os.pathsep + environment.get('PATH', '')
            if runtime.get('require_gpu'):
                devices = subprocess.run([str(binary), '--list-devices'], capture_output=True, timeout=15,
                    env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                if b'CUDA0' not in devices.stdout + devices.stderr:
                    raise RetrievalUnavailable('GPU运行时不可用，未自动切为缓慢的CPU编码')
            _processes[key] = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if _processes[key].poll() is not None:
                    raise RetrievalUnavailable('本机检索模型启动失败')
                try:
                    with httpx.Client(trust_env=False, follow_redirects=False, timeout=2) as client:
                        self._identity(client, base, role)
                    return base
                except (httpx.HTTPError, ValueError, KeyError):
                    time.sleep(.25)
            raise RetrievalUnavailable('本机检索模型启动超时')

    def embed(self, texts, query=False):
        base = self.ensure('embedding')
        inputs = [('Instruct: ' + self.cfg['query_instruction'] + '\nQuery: ' + text) if query else text
                  for text in texts]
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=90) as client:
            response = client.post(base + '/v1/embeddings', json={
                'model': self.cfg['embedding']['model'], 'input': inputs, 'encoding_format': 'float'})
            response.raise_for_status()
            data = response.json()['data']
        if (len(data) != len(inputs) or any(type(item.get('index')) is not int for item in data)
                or sorted(item['index'] for item in data) != list(range(len(inputs)))):
            raise RetrievalUnavailable('编码模型返回的向量数量或编号不匹配')
        return [normalized_vector(item['embedding'], self.cfg['embedding'].get('dimension'))
                for item in sorted(data, key=lambda item: item['index'])]

    def rank(self, query, texts):
        base = self.ensure('reranker')
        rows = []
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=90) as client:
            # Bound total prompt tokens even on runtimes without rank-batch splitting.
            for offset in range(0, len(texts)):
                batch = texts[offset:offset + 1]
                response = client.post(base + '/v1/rerank', json={
                    'model': self.cfg['reranker']['model'], 'query': query, 'documents': batch,
                    'top_n': len(batch), 'return_documents': False})
                response.raise_for_status()
                data = response.json()
                items = data.get('results', data.get('data', []))
                if (any(type(item.get('index')) is not int for item in items)
                        or {item['index'] for item in items} != set(range(len(batch))) or len(items) != len(batch)):
                    raise RetrievalUnavailable('重排模型返回的编号无效')
                rows.extend({**item, 'index': item['index'] + offset} for item in items)
        if (len(rows) != len(texts) or {item.get('index') for item in rows} != set(range(len(texts)))
                or any(type(item.get('relevance_score')) not in (int, float)
                       or not math.isfinite(item['relevance_score']) for item in rows)):
            raise RetrievalUnavailable('重排模型返回的编号或分数无效')
        return sorted(rows, key=lambda item: (-item['relevance_score'], item['index']))
