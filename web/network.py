"""Public web discovery and bounded crawling; candidates are not evidence."""
import asyncio
from collections import deque
from datetime import datetime
import hashlib
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import httpx
from ddgs import DDGS

from .service import kb, ws

DEFAULTS = {'provider': 'ddgs', 'backend': 'bing', 'searxng_url': '', 'region': 'cn-zh'}
USER_AGENT = 'IndustryAtlas/1.0 public-research'


def canonical_url(value):
    from .agent_runtime import source_url
    url = urlsplit(source_url(value))
    query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    return urlunsplit((url.scheme, url.netloc, url.path or '/', urlencode(query), ''))


class Robots:
    def __init__(self, delay=1.0):
        self.rules = {}
        self.fetched_at = {}
        self.last_request = {}
        self.delay = delay
        self.locks = {}

    async def check(self, url):
        parts = urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        async with self.locks.setdefault(origin, asyncio.Lock()):
            await self._check(url)

    async def _check(self, url):
        from .agent_runtime import source_url, public_url
        parts = urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        loop = asyncio.get_running_loop()
        if loop.time() - self.fetched_at.get(origin, 0) > 3600:
            self.rules.pop(origin, None)
        if origin not in self.rules:
            target = origin + '/robots.txt'
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False,
                                         limits=httpx.Limits(max_keepalive_connections=0),
                                         headers={'User-Agent': USER_AGENT}) as client:
                for _ in range(5):
                    connection = await public_url(target)
                    async with client.stream('GET', connection or target, headers={'Host': urlsplit(target).netloc},
                                             extensions={'sni_hostname': urlsplit(target).hostname}) as response:
                        if response.is_redirect:
                            from urllib.parse import urljoin
                            target = source_url(urljoin(target, response.headers.get('location', '')))
                            if urlsplit(target).netloc != parts.netloc:
                                raise ValueError('robots.txt重定向到其他站点，未抓取')
                            continue
                        if response.status_code in (404, 410):
                            text = ''
                        elif response.status_code in (401, 403):
                            text = 'User-agent: *\nDisallow: /'
                        else:
                            response.raise_for_status()
                            raw = bytearray()
                            async for chunk in response.aiter_bytes():
                                raw.extend(chunk)
                                if len(raw) > 512_000:
                                    raise ValueError('robots.txt超过大小上限，未抓取')
                            text = raw.decode('utf-8', errors='replace')
                        rule = RobotFileParser()
                        rule.parse(text.splitlines())
                        self.rules[origin] = rule
                        self.fetched_at[origin] = loop.time()
                        break
                else:
                    raise ValueError('robots.txt重定向过多，未抓取')
        rule = self.rules[origin]
        if not rule.can_fetch(USER_AGENT, url):
            raise ValueError('robots.txt不允许采集该页面')
        delay = max(self.delay, rule.crawl_delay(USER_AGENT) or rule.crawl_delay('*') or 0)
        if delay > 30:
            raise ValueError('站点要求较长抓取间隔，请缩小任务并单独安排')
        loop = asyncio.get_running_loop()
        remaining = delay - (loop.time() - self.last_request.get(origin, 0))
        if remaining > 0:
            await asyncio.sleep(remaining)
        self.last_request[origin] = loop.time()


class NetworkRuntime:
    def __init__(self, store):
        self.store = store
        self.path = store.root / 'config/network.json'
        self.jobs = {}
        self.tasks = {}
        self.pages = {}  # Bounded, RAM only: no entire article copies on disk.
        self.robots = Robots()  # Limits apply across concurrent batches too.
        for path in sorted((store.root / 'reports/crawls').glob('*.json'), key=lambda p: p.stat().st_mtime)[-20:]:
            job = kb.read_json(path)
            if job.get('status') == 'running':
                job['status'] = 'interrupted'
            self.jobs[job['id']] = job

    def config(self):
        return {**DEFAULTS, **(kb.read_json(self.path) if self.path.exists() else {})}

    def save_config(self, payload):
        from .agent_runtime import model_url
        config = {**self.config(), **{k: v for k, v in payload.items() if k in DEFAULTS}}
        if config['provider'] not in ('ddgs', 'searxng'):
            raise ValueError('请选择DDGS或SearXNG搜索后端')
        if config['backend'] not in ('bing', 'duckduckgo', 'brave'):
            raise ValueError('请选择支持的搜索引擎')
        if config['region'] not in ('cn-zh', 'us-en', 'wt-wt'):
            raise ValueError('搜索地区无效')
        if config['searxng_url']:
            config['searxng_url'] = model_url(config['searxng_url'])
        if config['provider'] == 'searxng' and not config['searxng_url']:
            raise ValueError('请填写SearXNG实例地址，并启用JSON搜索接口')
        with kb.lock(self.store.root):
            kb.atomic_write(self.path, kb.json_text(config))
        return config

    async def search(self, industry, query, limit=10, period=None):
        from .agent_runtime import allowed_source, source_url
        self.store.entry(industry)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise ValueError('搜索关键词应为1至500字')
        if type(limit) is not int or not 1 <= limit <= 30 or period not in (None, '', 'd', 'w', 'm', 'y'):
            raise ValueError('搜索数量或时间范围无效')
        catalog = self.store.sources(industry)
        sources = [s for s in catalog['sources'] if s.get('enabled', True)]
        domains = list(dict.fromkeys(urlsplit(s['url']).hostname for s in sources))
        submitted = query.strip()
        if catalog.get('mode') == 'only':
            if not domains:
                raise ValueError('限定来源模式尚无启用的来源，请先添加来源')
            submitted += ' (' + ' OR '.join('site:' + d for d in domains[:15]) + ')'
        config = self.config()
        try:
            if config['provider'] == 'ddgs':
                def call():
                    with DDGS(timeout=12) as engine:
                        return engine.text(submitted, region=config['region'], safesearch='moderate',
                                           timelimit=period or None, max_results=min(60, limit * 3), backend=config['backend'])
                raw = await asyncio.wait_for(asyncio.to_thread(call), timeout=45)
            else:
                async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
                    response = await client.get(config['searxng_url'] + '/search', params={
                        'q': submitted, 'format': 'json', 'categories': 'general',
                        'time_range': {'d': 'day', 'w': 'week', 'm': 'month', 'y': 'year'}.get(period, '')})
                    response.raise_for_status()
                    raw = response.json().get('results', [])
        except Exception as exc:
            # Never mistake search failure for absence of industry facts.
            raise ValueError('网页搜索未完成（' + type(exc).__name__ + '），请重试或切换搜索后端；不能据此判断没有资料') from None
        items, seen = [], set()
        for item in raw:
            try:
                url = source_url(item.get('href') or item.get('url') or '')
                key = canonical_url(url)
            except (ValueError, httpx.InvalidURL):
                continue
            if key in seen or not allowed_source(url, catalog):
                continue
            seen.add(key)
            preferred = any(urlsplit(url).hostname == d for d in domains)
            items.append({'title': str(item.get('title', url))[:300], 'url': url,
                          'snippet': str(item.get('body') or item.get('content') or '')[:700],
                          'preferred': preferred, 'state': 'unverified'})
        items.sort(key=lambda item: not item['preferred'])
        return {'query': query, 'submitted_query': submitted, 'provider': config['provider'],
                'backend': config['backend'] if config['provider'] == 'ddgs' else 'searxng',
                'searched_at': kb.now(), 'items': items[:limit], 'filtered': len(raw) - len(items),
                'note': '搜索摘要仅为线索；时间范围由引擎处理，实际发布日期须打开原文核验。'}

    def prepare(self, industry, urls=None, max_pages=10, max_depth=1):
        from .agent_runtime import allowed_source
        self.store.entry(industry)
        if type(max_pages) is not int or not 1 <= max_pages <= 50 or type(max_depth) is not int or not 0 <= max_depth <= 3:
            raise ValueError('每批1至50页，深度0至3')
        catalog = self.store.sources(industry)
        if urls is None or urls == []:
            urls = [s['url'] for s in sorted(catalog['sources'], key=lambda s: s.get('priority', 3)) if s.get('enabled', True)]
        if not isinstance(urls, list) or not 1 <= len(urls) <= 25 or not all(isinstance(u, str) for u in urls):
            raise ValueError('需要1至25个公开入口；可填写URL或先启用信息来源')
        urls = list(dict.fromkeys(canonical_url(u) for u in urls))
        if any(not allowed_source(u, catalog) for u in urls):
            raise ValueError('采集入口超出限定来源目录')
        return urls, catalog

    def start(self, industry, urls=None, max_pages=10, max_depth=1):
        seeds, catalog = self.prepare(industry, urls, max_pages, max_depth)
        if sum(j['status'] == 'running' for j in self.jobs.values()) >= 2:
            raise ValueError('最多同时运行两批采集，请等待或停止一批')
        # Retain 20 jobs and their RAM page sets, preventing unbounded memory use.
        for key in list(self.jobs):
            if len(self.jobs) < 20:
                break
            if self.jobs[key]['status'] != 'running':
                self.jobs.pop(key)
                self.pages.pop(key, None)
                self.tasks.pop(key, None)
        job = {'id': kb.uuid4().hex, 'industry': industry, 'seeds': seeds, 'max_pages': max_pages,
               'max_depth': max_depth, 'status': 'running', 'started_at': kb.now(), 'items': [],
               'attempted': 0, 'counts': {}, 'note': '采集候选不等于事实证据或完整行业更新。'}
        self.jobs[job['id']] = job
        self.pages[job['id']] = {}
        self.tasks[job['id']] = asyncio.create_task(self.run(job, catalog))
        return job

    async def run(self, job, catalog):
        from .agent_runtime import fetch_page, allowed_source
        queue = deque((url, 0) for url in job['seeds'])
        enqueued = set(job['seeds'])
        hosts = {urlsplit(u).netloc for u in job['seeds']}
        hashes = set()
        robots = self.robots
        home = self.store.home(job['industry'])
        index_path = home / 'data/crawl-index.json'
        old = kb.read_json(index_path) if index_path.exists() else {}
        updates = {}
        try:
            while queue and job['attempted'] < job['max_pages']:
                url, depth = queue.popleft()
                job['attempted'] += 1
                item = {'url': url, 'depth': depth}
                try:
                    async def guard(target):
                        if urlsplit(target).netloc not in hosts:
                            raise ValueError('重定向超出本批入口站点')
                        await robots.check(target)
                    for attempt in range(2):
                        try:
                            page = await fetch_page(url, catalog, guard=guard)
                            break
                        except (httpx.TimeoutException, httpx.NetworkError):
                            if attempt:
                                raise
                            await asyncio.sleep(1)
                    digest = hashlib.sha256(page['text'].encode('utf-8')).hexdigest()
                    canonical = canonical_url(page['url'])
                    previous = old.get(canonical, {})
                    known_duplicate = any(v.get('hash') == digest and k != canonical for k, v in old.items())
                    state = 'duplicate' if digest in hashes else 'unchanged' if previous.get('hash') == digest else 'changed' if previous else 'duplicate' if known_duplicate else 'new'
                    hashes.add(digest)
                    item.update(url=page['url'], title=page['title'] or page['url'], state=state,
                                checked_date=page['checked_date'], excerpt=page['text'][:360], hash=digest,
                                truncated=page['truncated'])
                    updates[canonical] = {k: item[k] for k in ('hash', 'title', 'checked_date')}
                    if state != 'duplicate':
                        self.pages[job['id']][url] = page
                        self.pages[job['id']][page['url']] = page
                    if depth < job['max_depth']:
                        for link in page['links']:
                            try:
                                target = canonical_url(link['url'])
                            except ValueError:
                                continue
                            if urlsplit(target).path.lower().endswith(('.pdf', '.zip', '.png', '.jpg', '.jpeg', '.mp4', '.xlsx', '.docx', '.css', '.js')):
                                continue
                            if any(part in urlsplit(target).path.lower().split('/') for part in ('cdn-cgi', 'login', 'logout', 'signin', 'signout')):
                                continue
                            if target not in enqueued and urlsplit(target).netloc in hosts and allowed_source(target, catalog):
                                if len(enqueued) >= 500:
                                    break
                                enqueued.add(target)
                                queue.append((target, depth + 1))
                except Exception as exc:
                    item.update(state='failed', error=(type(exc).__name__ + '：' + (str(exc) or '连接或读取未完成'))[:240])
                job['items'].append(item)
                job['counts'] = {state: sum(i['state'] == state for i in job['items']) for state in ('new', 'changed', 'unchanged', 'duplicate', 'failed')}
            success = job['attempted'] - job['counts'].get('failed', 0)
            job['status'] = 'failed' if not success else 'partial' if job['counts'].get('failed') else 'completed'
            job['remaining'] = len(queue)
        except asyncio.CancelledError:
            job['status'] = 'cancelled'
        except Exception as exc:
            job['status'] = 'failed'
            job['error'] = type(exc).__name__ + '：采集任务未完成'
        finally:
            job['finished_at'] = kb.now()
            try:
                with kb.lock(self.store.root):
                    current = kb.read_json(index_path) if index_path.exists() else {}
                    current.update(updates)
                    kb.atomic_write(index_path, kb.json_text(current))
                    kb.atomic_write(self.store.root / 'reports/crawls' / (job['id'] + '.json'), kb.json_text(job))
            except (OSError, ValueError):
                job['status'] = 'partial' if updates else 'failed'
                job['error'] = '采集结果仍在内存，但记录写入失败；请检查写入锁或磁盘后重试'

    def get(self, key):
        if key not in self.jobs:
            raise ValueError('采集任务不存在')
        return self.jobs[key]

    async def crawl(self, industry, urls=None, max_pages=10, max_depth=1):
        job = self.start(industry, urls, max_pages, max_depth)
        task = self.tasks[job['id']]
        try:
            await task
        except asyncio.CancelledError:
            task.cancel()
            raise
        return job
