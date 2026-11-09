"""Windows desktop entry point. Application code and writable data stay separate."""
import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / 'scripts'))
import kb
import workspace as ws

VERSION = '0.1.5'


def user_home():
    preferred = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'YanZhi'
    candidates = [preferred]
    if getattr(sys, 'frozen', False):
        candidates.append(Path(sys.executable).resolve().parent / 'UserData')
    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ('write-check-' + kb.uuid4().hex)
            with probe.open('x', encoding='utf-8') as handle:
                handle.write('1')
            probe.unlink()
            return folder
        except OSError:
            continue
    raise OSError('用户数据目录不可写，请把程序安装到自己可写的文件夹。')


def display_root(root):
    value = str(Path(root).resolve())
    if value.startswith('\\\\?\\UNC\\'):
        return '\\\\' + value[8:]
    return value[4:] if value.startswith('\\\\?\\') else value


def storage_root(root):
    """Extended paths also work when Windows' machine-wide LongPathsEnabled is off."""
    value = str(Path(root).resolve())
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)
    return Path(value)


def root_id(root):
    return hashlib.sha256(os.path.normcase(display_root(root)).encode()).hexdigest()


def initialize_library(root):
    """Create templates only in a new directory; never repair/overwrite a live library."""
    root = storage_root(root)
    if (root / 'config/industries.json').is_file():
        ws.registry(root)
        for required in ('agent.json', 'automation.json'):
            if not (root / 'config' / required).is_file():
                raise ValueError('现有知识库缺少 config/' + required)
        return root
    if root.exists() and any(root.iterdir()):
        raise ValueError('请选择已有知识库根目录，或一个空目录。不会覆盖已有文件。')
    root.mkdir(parents=True, exist_ok=True)
    kb.atomic_write(root / 'config/industries.json', kb.json_text(
        {'version': 1, 'active_industry': None, 'industries': []}))
    kb.atomic_write(root / 'config/automation.json', kb.json_text(
        {'status': 'PAUSED', 'name': '自动采集未启用', 'execution': '桌面应用不创建定时采集任务。'}))
    kb.atomic_write(root / 'config/agent.json', kb.json_text({
        'provider_id': '', 'display_name': '',
        'base_url': '', 'protocol': 'chat_completions',
        'model': '', 'api_key_env': 'KB_MODEL_API_KEY',
        'request_limit': 30, 'timeout_seconds': 300, 'scheduler_enabled': False, 'daily_time': '09:00'}))
    return root


class LibraryLease:
    """OS releases this byte lock after a crash; a stale JSON file cannot block startup."""
    def __init__(self, root):
        self.path = Path(root) / 'data/desktop.lock'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.handle.write(b'0')
            self.handle.flush()
        self.handle.seek(0)
        try:
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except Exception:
            self.handle.close()
            raise ValueError('该知识库已由另一个桌面窗口打开，请使用原窗口。') from None

    def close(self):
        if not self.handle.closed:
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()


def fetch_json(url):
    # Ignore proxy environment for loopback readiness checks.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2) as response:
        return json.load(response)


def running_url(root):
    try:
        info = kb.read_json(Path(root) / 'data/desktop-server.json')
        port = int(info['port'])
        if not 1024 <= port <= 65535:
            return None
        url = f'http://127.0.0.1:{port}'
        status = fetch_json(url + '/api/desktop/status')
        if status.get('root_id') == root_id(root) and status.get('app') == 'YanZhi':
            return url
    except (OSError, ValueError, KeyError):
        pass
    return None


class DesktopServer:
    def __init__(self, root, port=8766):
        self.root = initialize_library(root)
        self.lease = LibraryLease(self.root)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            try:
                self.socket.bind(('127.0.0.1', port))
            except OSError:
                if not port:
                    raise
                self.socket.bind(('127.0.0.1', 0))
            self.socket.listen(128)
            self.port = self.socket.getsockname()[1]
            self.url = f'http://127.0.0.1:{self.port}'
            previous_root = os.environ.get('KB_LIBRARY_ROOT')
            os.environ['KB_LIBRARY_ROOT'] = str(self.root)
            try:
                from web.server import create_app
            finally:
                if previous_root is None:
                    os.environ.pop('KB_LIBRARY_ROOT', None)
                else:
                    os.environ['KB_LIBRARY_ROOT'] = previous_root
            import uvicorn
            app = create_app(self.root)

            @app.get('/api/desktop/status')
            def desktop_status():
                return {'app': 'YanZhi', 'version': VERSION, 'root_id': root_id(self.root)}

            config = uvicorn.Config(app, host='127.0.0.1', port=self.port, log_config=None,
                                    access_log=False, loop='asyncio', http='h11', ws='none')
            self.server = uvicorn.Server(config)
            self.thread = threading.Thread(target=self._run, daemon=True, name='YanZhiServer')
            self.error = None
            self.thread.start()
        except BaseException:
            self.socket.close()
            self.lease.close()
            raise

    def _run(self):
        try:
            self.server.run(sockets=[self.socket])
        except BaseException as exc:
            self.error = type(exc).__name__

    def wait_ready(self, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.thread.is_alive():
                raise RuntimeError('本地服务启动失败：' + (self.error or '服务已退出'))
            if self.server.started:
                status = fetch_json(self.url + '/api/desktop/status')
                if status.get('root_id') == root_id(self.root):
                    kb.atomic_write(self.root / 'data/desktop-server.json', kb.json_text(
                        {'pid': os.getpid(), 'port': self.port, 'version': VERSION}))
                    return self.url
            time.sleep(0.1)
        raise RuntimeError('本地服务启动超时，请查看启动日志。')

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=20)
        if self.thread.is_alive():
            raise RuntimeError('服务正在处理任务，请稍后再关闭窗口。')
        self.socket.close()
        (self.root / 'data/desktop-server.json').unlink(missing_ok=True)
        self.lease.close()


def self_test(report):
    """Exercise the frozen binary with synthetic data, without a model or internet."""
    import tempfile
    import sqlite3
    import asyncio
    import tkinter as tk
    from tkinter import ttk
    tk_root = tk.Tk()
    tk_root.withdraw()
    ttk.Label(tk_root, text='研知启动器测试').pack()
    tk_root.update()
    tk_root.destroy()
    with tempfile.TemporaryDirectory(prefix='yanzhi-selftest-', dir=storage_root(tempfile.gettempdir())) as folder:
        root = Path(folder) / '知识库 测试'
        server = DesktopServer(root, port=0)
        try:
            url = server.wait_ready()
            state = fetch_json(url + '/api/state')
            fresh_blank = (state['industries'] == [] and state['active_industry'] is None
                and not state['agent']['configured'] and not state['agent']['key_configured']
                and all(state['agent'][key] == '' for key in ('provider_id', 'display_name', 'base_url', 'model')))
            ws.industry_command(root, argparse.Namespace(action='add', id='packaging-smoke',
                name='打包自检行业', region='测试地区', categories=None))
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + '/') as response:
                html = response.read().decode('utf-8')
            with sqlite3.connect(':memory:') as conn:
                conn.execute('CREATE VIRTUAL TABLE smoke USING fts5(text)')
            from ddgs import DDGS
            from pypdf import PdfReader
            from web.knowledge import Knowledge
            from web.verification import VerificationQueue
            from web.material_index import MaterialIndex
            store = server.server.config.app.state.store
            knowledge = Knowledge(store)
            material = knowledge.save('packaging-smoke', {
                'id': 'desktop-smoke-material', 'title': '打包测试', 'kind': 'background',
                'region': '测试地区', 'summary': '仅用于程序打包测试', 'content': '这是本地合成测试文字。',
                'model_allowed': True})
            queue = VerificationQueue(store)
            review = queue.prepare_material('packaging-smoke', material['id'])
            queue.approve_material('packaging-smoke', review['id'], {'decision': 'approve'})
            index = MaterialIndex(store)
            index.build('packaging-smoke', material['id'])
            found = index.search('packaging-smoke', '合成测试')
            async def model_smoke():
                import httpx
                from openai import AsyncOpenAI
                from pydantic_ai import Agent
                from web.agent_runtime import configured_model, configured_model_settings
                def reply(request):
                    return httpx.Response(200, json={'id': 'desktop-smoke', 'object': 'chat.completion',
                        'created': 1, 'model': 'synthetic-model', 'choices': [{'index': 0,
                        'message': {'role': 'assistant', 'content': '安装自检通过'}, 'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
                async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                    sdk = AsyncOpenAI(api_key='synthetic-test-key', base_url='http://127.0.0.1:1/v1', http_client=client)
                    config = {**kb.read_json(root / 'config/agent.json'),
                        'provider_id': 'synthetic-test', 'base_url': 'https://model.example/v1',
                        'model': 'synthetic-model'}
                    agent = Agent(configured_model(config, sdk), model_settings=configured_model_settings(config))
                    return (await agent.run('仅用于本地合成测试')).output == '安装自检通过'
            model_ok = asyncio.run(model_smoke())
            result = {'ok': True, 'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
                'active_industry': state['active_industry'], 'profiles': len(state['industries']),
                'evidence_count': sum(item['evidence_count'] for item in state['industries']),
                'all_collection_disabled': all(not item['enabled'] for item in state['industries']),
                'fresh_blank': fresh_blank, 'html_loaded': '<html' in html.lower(), 'fts5': True, 'tk_gui_loaded': True,
                'material_saved': material['id'] == 'desktop-smoke-material',
                'fulltext_search': bool(found),
                'synthetic_model_roundtrip': model_ok,
                'ddgs_imported': DDGS is not None, 'pdf_imported': PdfReader is not None}
            if not all(result[k] for k in ('fresh_blank', 'html_loaded', 'all_collection_disabled', 'material_saved', 'fulltext_search', 'synthetic_model_roundtrip')):
                raise RuntimeError('自检不通过')
        finally:
            server.stop()
    kb.atomic_write(Path(report), kb.json_text(result))


def gui(root_path, auto_start=True):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    window = tk.Tk()
    window.title('研知 · 行业知识库')
    window.geometry('620x315')
    window.minsize(620, 315)
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='研知 · 行业知识库', font=('Microsoft YaHei UI', 18)).pack(anchor='w')
    ttk.Label(frame, text='本机运行 · 浏览器工作台', padding=(0, 8)).pack(anchor='w')
    path_value = tk.StringVar(value=str(root_path))
    ttk.Entry(frame, textvariable=path_value, state='readonly').pack(fill='x', pady=(8, 6))
    status = tk.StringVar(value='准备启动')
    ttk.Label(frame, textvariable=status, wraplength=565).pack(anchor='w', pady=8)
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', pady=8)
    active = {'server': None, 'starting': False, 'closing': False}

    def set_buttons():
        busy = active['starting'] or active['closing']
        running = active['server'] is not None
        open_button.configure(state='disabled' if busy else 'normal')
        choose_button.configure(state='disabled' if busy or running else 'normal')
        new_button.configure(state='disabled' if busy or running else 'normal')
        stop_button.configure(state='normal' if running and not busy else 'disabled')

    def finish_start(server, error):
        active['starting'] = False
        active['server'] = server
        if error:
            status.set('启动失败：' + error)
            messagebox.showerror('启动失败', error, parent=window)
        else:
            status.set('运行中：' + server.url + '　关闭启动器将停止服务')
            kb.atomic_write(user_home() / 'desktop.json', kb.json_text({'library': display_root(server.root)}))
            webbrowser.open(server.url)
        set_buttons()

    def start():
        if active['server']:
            webbrowser.open(active['server'].url)
            return
        current = Path(path_value.get())
        existing = running_url(current)
        if existing:
            webbrowser.open(existing)
            status.set('已打开另一个启动器管理的本地服务：' + existing)
            return
        active['starting'] = True
        status.set('正在启动本地服务…')
        set_buttons()
        def worker():
            server = None
            try:
                server = DesktopServer(current)
                server.wait_ready()
                window.after(0, lambda: finish_start(server, None))
            except Exception as exc:
                if server:
                    server.stop()
                error = str(exc)
                window.after(0, lambda: finish_start(None, error))
        threading.Thread(target=worker, daemon=True).start()

    def choose():
        folder = filedialog.askdirectory(title='选择知识库根目录，或用于新建的空目录', parent=window)
        if folder:
            path_value.set(folder)
            status.set('点击“打开工作台”启动所选知识库')

    def new_library():
        folder = filedialog.askdirectory(title='选择或新建一个空目录，原知识库会保留', parent=window)
        if not folder:
            return
        try:
            if any(Path(folder).iterdir()):
                messagebox.showerror('目录不为空', '请选择或新建一个空目录；现有资料不会被删除。', parent=window)
                return
        except OSError as exc:
            messagebox.showerror('无法读取目录', str(exc), parent=window)
            return
        path_value.set(folder)
        status.set('点击“打开工作台”创建空白库，行业与模型均需自行配置')

    def stop(close=False):
        if active['starting'] or active['closing']:
            return
        if not active['server']:
            if close:
                window.destroy()
            return
        active['closing'] = True
        status.set('正在停止服务，请稍候…')
        set_buttons()
        def worker():
            error = None
            try:
                active['server'].stop()
            except Exception as exc:
                error = str(exc)
            def finish():
                active['closing'] = False
                if error:
                    status.set(error)
                else:
                    active['server'] = None
                    status.set('已停止，可选择其他知识库')
                    if close:
                        window.destroy()
                        return
                set_buttons()
            window.after(0, finish)
        threading.Thread(target=worker, daemon=True).start()

    open_button = ttk.Button(buttons, text='打开工作台', command=start)
    open_button.pack(side='left', padx=(0, 10))
    choose_button = ttk.Button(buttons, text='选择知识库目录', command=choose)
    choose_button.pack(side='left', padx=(0, 10))
    new_button = ttk.Button(buttons, text='新建空白库', command=new_library)
    new_button.pack(side='left', padx=(0, 10))
    stop_button = ttk.Button(buttons, text='停止服务', command=stop)
    stop_button.pack(side='left')
    ttk.Label(frame, text='资料保存在所选目录；模型接口可在工作台设置中配置。', padding=(0, 8)).pack(anchor='w')
    set_buttons()
    window.protocol('WM_DELETE_WINDOW', lambda: stop(close=True))
    if auto_start:
        window.after(150, start)
    window.mainloop()


def main():
    parser = argparse.ArgumentParser(description='研知桌面启动器')
    parser.add_argument('--library', type=Path)
    parser.add_argument('--self-test', type=Path, metavar='REPORT_JSON')
    parser.add_argument('--no-autostart', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        try:
            self_test(args.self_test)
            return 0
        except BaseException as exc:
            import traceback
            kb.atomic_write(args.self_test, kb.json_text({'ok': False, 'error_type': type(exc).__name__,
                'error': str(exc), 'traceback': traceback.format_exc()}))
            return 1
    home = user_home()
    home.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(handlers=[RotatingFileHandler(home / 'desktop.log', maxBytes=1_000_000,
        backupCount=2, encoding='utf-8')], level=logging.WARNING)
    root = args.library
    if root is None:
        try:
            root = Path(kb.read_json(home / 'desktop.json')['library'])
        except (OSError, ValueError, KeyError):
            root = home / 'KnowledgeBase'
    existing = running_url(root)
    if existing:
        webbrowser.open(existing)
        return 0
    gui(root, auto_start=not args.no_autostart)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
