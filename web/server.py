"""Loopback-only web app for the industry knowledge base."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from .service import Store, kb, ws
from .agent_runtime import AgentRuntime
from .network import NetworkRuntime
from .knowledge import Knowledge, KINDS, STATES

WEB = Path(__file__).resolve().parent


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH'):
            return await self.app(scope, receive, send)
        limit = 12_000_000 if scope['path'].startswith('/api/materials/') and scope['path'].endswith('/upload') else 1_000_000
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > limit:
                return await JSONResponse({'detail': '请求超过大小上限'}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get('more_body', False):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()
        return await self.app(scope, replay, send)


class IndustryCreate(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=100)
    region: str = Field(default='中国', min_length=1, max_length=100)
    categories: list[str] | None = None
    specialty: Literal['forestry'] | None = None


class AgentRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    industry: str = 'all'
    research: bool = False
    conversation: str | None = None
    allow_source_changes: bool = False
    crawl_id: str | None = None


def create_app(root=None):
    store = Store(root) if root else Store()
    network = NetworkRuntime(store)
    runtime = AgentRuntime(store, network)
    knowledge = Knowledge(store)

    @asynccontextmanager
    async def lifespan(app):
        yield
        tasks = [*runtime.tasks.values(), *network.tasks.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title='Industry Atlas', lifespan=lifespan)
    app.state.store = store
    app.state.runtime = runtime
    app.state.network = network
    app.state.knowledge = knowledge
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'])
    app.add_middleware(BodyLimit)

    @app.middleware('http')
    async def local_request(request, call_next):
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            if request.headers.get('x-kb-request') != '1':
                return JSONResponse({'detail': '需要本地页面请求标识'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': '拒绝跨站写入'}, status_code=403)
            length = request.headers.get('content-length')
            if length:
                try:
                    if int(length) < 0:
                        raise ValueError()
                except ValueError:
                    return JSONResponse({'detail': '请求长度格式无效'}, status_code=400)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(OSError)
    async def file_error(request, exc):
        return JSONResponse({'detail': '文件不可读或无法保存，请检查知识库路径和写入锁'}, status_code=409)

    @app.get('/')
    def index():
        return FileResponse(WEB / 'static/index.html')

    @app.get('/api/state')
    def state():
        return {**store.state(), 'agent': runtime.public_config(), 'network': network.config(),
                'knowledge': knowledge.settings(), 'material_kinds': KINDS, 'material_states': STATES}

    @app.get('/api/materials')
    def materials(industry: str = 'all', q: str = '', kind: str = ''):
        return [{k: v for k, v in r.items() if k != 'content'} for r in knowledge.list(industry, q, kind)]

    @app.post('/api/materials/{industry}')
    def material_save(industry: str, payload: dict):
        return knowledge.save(industry, payload)

    @app.get('/api/materials/{industry}/{key}')
    def material_get(industry: str, key: str):
        return knowledge.display(knowledge.get(industry, key))

    @app.post('/api/materials/{industry}/{key}/upload')
    async def material_upload(industry: str, key: str, file: UploadFile = File(...)):
        raw = await file.read(10_000_001)
        await file.close()
        try:
            return await asyncio.to_thread(knowledge.upload, industry, key, file.filename or 'document.txt', raw)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('文档无法提取（' + type(exc).__name__ + '），请检查文件格式或使用文字版') from None

    @app.get('/api/materials/{industry}/{key}/file')
    def material_file(industry: str, key: str):
        path, name = knowledge.attachment(industry, key)
        return FileResponse(path, filename=name)

    @app.get('/api/materials/{industry}/{key}/sections')
    def material_sections(industry:str,key:str,q:str='',offset:int=0,limit:int=5):
        return knowledge.sections(industry,key,q,offset,limit)

    @app.get('/api/memories')
    def memories(industry: str = 'all'):
        return knowledge.memories(industry)

    @app.post('/api/memories')
    def memory_save(payload: dict):
        return knowledge.save_memory(payload)

    @app.post('/api/memories/{key}/archive')
    def memory_archive(key: str):
        return knowledge.archive_memory(key)

    @app.put('/api/knowledge/settings')
    def knowledge_settings(payload: dict):
        return knowledge.save_settings(payload)

    @app.get('/api/conversations')
    def conversations(industry: str = 'all'):
        return knowledge.conversations(industry)

    @app.put('/api/network/config')
    def network_config(payload: dict):
        return network.save_config(payload)

    @app.post('/api/network/search')
    async def network_search(payload: dict):
        return await network.search(payload.get('industry'), payload.get('query'), payload.get('limit', 10), payload.get('period'))

    @app.post('/api/crawls')
    async def crawl_start(payload: dict):
        return network.start(payload.get('industry'), payload.get('urls'), payload.get('max_pages', 10), payload.get('max_depth', 1))

    @app.get('/api/crawls')
    def crawls(industry: str):
        store.entry(industry)
        return [j for j in reversed(list(network.jobs.values())) if j['industry'] == industry]

    @app.get('/api/crawls/{job_id}')
    def crawl_job(job_id: str):
        return network.get(job_id)

    @app.post('/api/crawls/{job_id}/stop')
    async def crawl_stop(job_id: str):
        network.get(job_id)
        task = network.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        return {'ok': True}

    @app.get('/api/evidence')
    def evidence(industry: str = 'all', q: str = '', category: str | None = None, freshness: str | None = None,
                 status: str | None = None, tag: str | None = None, source_id: str | None = None, review_only: bool = False,
                 include_shared: bool = True):
        return store.evidence(industry, q, category=category or None, freshness=freshness or None,
                              status=status or None, tag=tag or None, source_id=source_id or None, review_only=review_only, limit=100,
                              include_shared=include_shared)

    @app.get('/api/evidence/{industry}/{evidence_id}')
    def record(industry: str, evidence_id: str):
        ws.safe_id(evidence_id)
        records = store.evidence(industry, limit=10**9)
        record = next((r for r in records if r['id'] == evidence_id), None)
        if not record:
            raise HTTPException(404, '证据不存在')
        history = sorted((store.home(industry) / 'history' / evidence_id).glob('*.json'), reverse=True)
        return {**record, 'history': [kb.read_json(p) for p in history[:20]]}

    @app.post('/api/evidence/{industry}')
    def import_evidence(industry: str, payload: dict):
        return store.import_evidence(industry, payload)

    @app.post('/api/industries')
    def industry_create(payload: IndustryCreate):
        return ws.industry_command(store.root, store.args(action='add', **payload.model_dump()))

    @app.put('/api/industries/{industry}')
    def industry_update(industry: str, payload: dict):
        store.entry(industry)
        if 'enabled' in payload:
            if type(payload['enabled']) is not bool:
                raise ValueError('enabled 必须是布尔值')
            ws.industry_command(store.root, store.args(action='enable' if payload['enabled'] else 'disable', id=industry))
        if payload.get('active'):
            ws.industry_command(store.root, store.args(action='use', id=industry))
        return store.state()

    @app.put('/api/active-industry')
    def active_update(payload: dict):
        return ws.industry_command(store.root, store.args(action='use', id=payload.get('id') or 'none'))

    @app.put('/api/scope/{industry}')
    def scope_update(industry: str, payload: dict):
        return store.save_scope(industry, payload)

    @app.get('/api/sources/{industry}')
    def sources(industry: str):
        return store.sources(industry)

    @app.put('/api/sources/{industry}')
    def sources_update(industry: str, payload: dict):
        return store.save_sources(industry, payload)

    @app.get('/api/reports')
    def reports():
        return store.documents()

    @app.get('/api/document')
    def document(path: str):
        return {'path': path, 'text': store.read_document(path)}

    @app.get('/api/download')
    def download(path: str):
        text = store.read_document(path)
        filename = Path(path).name
        from urllib.parse import quote
        return PlainTextResponse(text, headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename)})

    @app.post('/api/reports')
    def create_report(payload: dict):
        result = ws.brief(store.root, store.research_entries(payload.get('industry', 'all')),
                          store.args(query=payload.get('query') or None))
        result['path'] = Path(result['path']).relative_to(store.root).as_posix()
        return result

    @app.get('/api/update-plan')
    def plan():
        return ws.update_plan(store.root)

    @app.put('/api/agent/config')
    def agent_config(payload: dict):
        secret = payload.pop('api_key', None)
        if secret is not None and not isinstance(secret, str):
            raise ValueError('密钥格式错误')
        return runtime.save_config(payload, secret)

    @app.post('/api/agent/test')
    async def agent_test():
        try:
            return await runtime.test_connection()
        except Exception as exc:
            return {'ok': False, 'message': type(exc).__name__ + '：连接未通过，请检查地址和后端服务。'}

    @app.post('/api/agent/jobs')
    async def agent_start(payload: AgentRequest):
        return runtime.start(payload.prompt, payload.industry, payload.research, payload.conversation,
                             payload.allow_source_changes, payload.crawl_id)

    @app.get('/api/agent/jobs/{job_id}')
    def job(job_id: str):
        if job_id not in runtime.jobs:
            raise HTTPException(404, '任务不存在或服务已重启')
        return runtime.jobs[job_id]

    @app.post('/api/agent/jobs/{job_id}/stop')
    async def stop_job(job_id: str):
        task = runtime.tasks.get(job_id)
        if not task:
            raise HTTPException(404, '任务不存在')
        task.cancel()
        return {'ok': True}

    @app.get('/api/agent/jobs/{job_id}/events')
    async def events(job_id: str, request: Request):
        if job_id not in runtime.jobs:
            raise HTTPException(404, '任务不存在')
        async def stream():
            seen = int(request.headers.get('last-event-id', '-1')) + 1
            job = runtime.jobs[job_id]
            while True:
                if await request.is_disconnected():
                    break
                for event in job['events'][seen:]:
                    yield f'id: {event["seq"]}\nevent: update\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
                    seen += 1
                if job['status'] != 'running' and seen >= len(job['events']):
                    break
                yield ': heartbeat\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type='text/event-stream')

    app.mount('/static', StaticFiles(directory=WEB / 'static'), name='static')
    return app


app = create_app()

if __name__ == '__main__':
    import uvicorn
    kb.atomic_write(app.state.store.root / 'data/web-server.pid', str(os.getpid()) + '\n')
    uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('KB_WEB_PORT', '8765')))
