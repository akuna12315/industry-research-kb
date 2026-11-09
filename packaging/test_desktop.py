"""Desktop lifecycle and data isolation regression tests, using synthetic libraries."""
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from desktop_app import DesktopServer, LibraryLease, initialize_library, running_url, root_id, storage_root, user_home


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '空白 知识库'

    def test_new_library_has_no_facts_or_enabled_collection(self):
        initialize_library(self.root)
        registry = json.loads((self.root / 'config/industries.json').read_text(encoding='utf-8'))
        self.assertIsNone(registry['active_industry'])
        self.assertEqual(registry['industries'], [])
        config = json.loads((self.root / 'config/agent.json').read_text(encoding='utf-8'))
        self.assertTrue(all(config[key] == '' for key in ('provider_id', 'display_name', 'base_url', 'model')))
        self.assertFalse((self.root / 'industries').exists())
        self.assertFalse(list(self.root.glob('**/evidence/*.json')))
        self.assertFalse(list(self.root.glob('**/materials/*.json')))

    def test_existing_configuration_is_preserved_byte_for_byte(self):
        initialize_library(self.root)
        config = self.root / 'config/agent.json'
        config.write_text('{"model":"user-selected-model"}', encoding='utf-8')
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        initialize_library(self.root)
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_blank_startup_and_first_configuration_need_no_presets(self):
        from fastapi.testclient import TestClient
        initialize_library(self.root)
        with patch.dict('os.environ', {'KB_LIBRARY_ROOT': str(self.root), 'KB_MODEL_API_KEY': 'synthetic-env-key'}):
            from web.server import create_app
            with TestClient(create_app(self.root), headers={'X-KB-Request': '1'}) as client:
                state = client.get('/api/state').json()
                self.assertEqual(state['industries'], [])
                self.assertFalse(state['agent']['configured'])
                self.assertFalse(state['agent']['key_configured'])
                reply = client.post('/api/agent/jobs', json={'prompt': '测试', 'industry': 'all'})
                self.assertEqual(reply.status_code, 400)
                self.assertIn('尚未配置模型', reply.json()['detail'])
                self.assertIn('尚未配置模型', client.post('/api/agent/test').json()['message'])
                saved = client.put('/api/agent/config', json={
                    'provider_id': 'synthetic', 'display_name': '自定义测试模型',
                    'base_url': 'https://model.example/v1', 'model': 'synthetic-model', 'api_key': 'synthetic-key'})
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertTrue(saved.json()['configured'])
                self.assertTrue(saved.json()['key_configured'])
                created = client.post('/api/industries', json={'id': 'custom-industry', 'name': '自定义行业', 'region': '测试地区'})
                self.assertEqual(created.status_code, 200, created.text)
                self.assertEqual(len(client.get('/api/state').json()['industries']), 1)

    def test_nonempty_unrecognized_directory_is_never_overwritten(self):
        self.root.mkdir()
        original = self.root / '重要资料.txt'
        original.write_text('保留原文', encoding='utf-8')
        with self.assertRaises(ValueError):
            initialize_library(self.root)
        self.assertEqual(list(self.root.iterdir()), [original])
        self.assertEqual(original.read_text(encoding='utf-8'), '保留原文')

    def test_duplicate_desktop_lock_and_release(self):
        initialize_library(self.root)
        lease = LibraryLease(self.root)
        try:
            with self.assertRaises(ValueError):
                LibraryLease(self.root)
        finally:
            lease.close()
        LibraryLease(self.root).close()

    def test_busy_port_falls_back_and_shutdown_releases_lock(self):
        occupied = socket.socket()
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        self.addCleanup(occupied.close)
        server = DesktopServer(self.root, port=occupied.getsockname()[1])
        try:
            server.wait_ready()
            self.assertNotEqual(server.port, occupied.getsockname()[1])
            self.assertEqual(running_url(self.root), server.url)
            with self.assertRaises(ValueError):
                DesktopServer(self.root, port=0)
        finally:
            server.stop()
        self.assertIsNone(running_url(self.root))
        LibraryLease(self.root).close()

    def test_startup_failure_releases_socket_and_lease(self):
        with patch('web.server.create_app', side_effect=ValueError('模拟启动失败')):
            with self.assertRaises(ValueError):
                DesktopServer(self.root, port=0)
        LibraryLease(self.root).close()

    def test_extended_library_paths_and_identity(self):
        root = initialize_library(self.root)
        self.assertEqual(root_id(root), root_id(self.root))
        target = storage_root(root / ('a' * 110) / ('b' * 110) / '保留原文.txt')
        target.parent.mkdir(parents=True)
        try:
            target.write_text('长路径原文', encoding='utf-8')
            self.assertGreater(len(str(target)), 260)
            self.assertEqual(target.read_text(encoding='utf-8'), '长路径原文')
        finally:
            target.unlink(missing_ok=True)
            target.parent.rmdir()
            target.parent.parent.rmdir()

    def test_frozen_user_home_falls_back_when_profile_is_not_writable(self):
        blocked = Path(self.temp.name) / 'not-a-directory'
        blocked.write_text('test', encoding='utf-8')
        app = Path(self.temp.name) / 'app'
        app.mkdir()
        with patch.dict('os.environ', {'LOCALAPPDATA': str(blocked)}), patch('sys.frozen', True, create=True), patch('sys.executable', str(app / 'YanZhi.exe')):
            self.assertEqual(user_home(), app / 'UserData')
        self.assertEqual(blocked.read_text(encoding='utf-8'), 'test')


if __name__ == '__main__':
    unittest.main()
