"""验证旧网址、条件缓存和版本切换实际取得同一套界面。"""
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from server import create_server, ProxyHandler


class UiDeliveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.quiet = patch.object(ProxyHandler, 'log_message', lambda *args: None)
        cls.quiet.start()
        cls.server = create_server(0, Path(cls.tmp.name) / 'bank.sqlite3')
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.quiet.stop()
        cls.tmp.cleanup()

    def test_legacy_url_preserves_mode_and_question_with_current_assets(self):
        with urllib.request.urlopen(self.base + '/chat.html?mode=youth&q=hello') as result:
            html = result.read().decode()
            self.assertIn('mode=youth', result.url)
            self.assertIn('q=hello', result.url)
            self.assertIn('ui=' + self.server.app_release, result.url)
            self.assertIn('<title>Navren', html)
            self.assertIn('/js/app_boot.js?v=' + self.server.app_release, html)
            self.assertIn('css/design.css?v=' + self.server.app_release, html)
            self.assertIn('no-store', result.headers['Cache-Control'])

    def test_future_modified_since_never_reuses_old_javascript(self):
        req = urllib.request.Request(self.base + '/js/design.js?v=old', headers={
            'If-Modified-Since': 'Wed, 31 Dec 2099 23:59:59 GMT',
            'If-None-Match': 'old',
        })
        with urllib.request.urlopen(req) as result:
            self.assertEqual(result.status, 200)
            self.assertIn('no-store', result.headers['Cache-Control'])
            self.assertIn('NavrenDesign', result.read().decode())

    def test_stale_revision_recovers_all_pages_and_version_endpoint(self):
        with urllib.request.urlopen(self.base + '/api/app-version') as result:
            version = json.load(result)
        self.assertEqual(version, {'name': 'Navren', 'release': self.server.app_release})
        for name in ('index', 'chat', 'recommend', 'account', 'mine', 'copilot'):
            with self.subTest(page=name), urllib.request.urlopen(
                    self.base + '/' + name + '.html?mode=classic&ui=previous-release') as result:
                self.assertIn('ui=' + version['release'], result.url)
                self.assertEqual(result.headers['X-Navren-Release'], version['release'])
                self.assertIn('navren-release', result.read().decode())


if __name__ == '__main__':
    unittest.main()
