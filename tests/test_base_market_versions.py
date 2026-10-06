"""真实本地 HTTP 校验公共市场详情的严格版本约束；全部数据库隔离。"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from scripts.verify_base_runtime import temporary_runtime


CURRENT_VERSION = 4
CURRENT_MARKER = "isolated-current-market-body-v4"
DETAIL_PATHS = (
    "/api/market/weeklies/2026-09-28",
    "/api/market/events/isolated-event",
    "/api/market/articles/isolated-article",
)


class _OfflineMarketDetails:
    """仅替换三种详情读取方法，保留原 MarketStore 及其临时数据库。"""

    def __init__(self):
        self.calls = []
        self.weekly_missing = False

    def _object(self, kind, identifier):
        self.calls.append((kind, identifier))
        return {"id": identifier, "version": CURRENT_VERSION,
                "status": "published", "body": CURRENT_MARKER}

    def event(self, identifier):
        return deepcopy(self._object("event", identifier))

    def article(self, identifier):
        return deepcopy(self._object("article", identifier))

    def weekly(self, period="latest"):
        report = self._object("weekly", period)
        report["period"] = "2026-09-28"
        return {"schema_version": 1, "status": "published", "history": [],
                "report": None if self.weekly_missing else deepcopy(report)}


def _database_files(path):
    """读取文件字节哈希，不使用会执行提交的 provider.db()。"""
    result = {}
    for candidate in (Path(path), Path(str(path) + "-wal")):
        if candidate.exists():
            result[candidate.name] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return result


class BaseMarketVersionHTTPTests(unittest.TestCase):
    observations = []

    @classmethod
    def setUpClass(cls):
        cls.runtime = temporary_runtime(initialize=False)
        cls.server, cls.base, imported = cls.runtime.__enter__()
        cls.addClassCleanup(cls.runtime.__exit__, None, None, None)
        assert imported is None
        cls.original_market = cls.server.market
        cls.market_files_before = _database_files(cls.original_market.path)
        cls.provider = _OfflineMarketDetails()
        cls.patches = ExitStack()
        cls.addClassCleanup(cls.patches.close)
        for name in ("event", "article", "weekly"):
            cls.patches.enter_context(patch.object(cls.original_market, name,
                getattr(cls.provider, name)))
        cls.observations = []

    def tearDown(self):
        self.assertIs(self.server.market, self.original_market)
        self.assertEqual(_database_files(self.original_market.path), self.market_files_before,
                         "公共详情请求不能改写原 provider 的数据库")

    def request(self, path):
        request = urllib.request.Request(self.base + path, method="GET")
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw)
            status = response.status
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.observations.append({"path": path, "http_status": status,
            "error_code": payload.get("error", {}).get("code"),
            "returned_version": (payload.get("report") or {}).get("version")
                if "/weeklies/" in path else payload.get("version"),
            "current_body_present": CURRENT_MARKER in raw})
        return status, payload, raw

    def assert_error(self, path, status, code):
        actual, payload, raw = self.request(path)
        self.assertEqual(actual, status, (path, payload))
        self.assertEqual(payload["error"]["code"], code, path)
        self.assertEqual(set(payload), {"error"}, path)
        self.assertNotIn(CURRENT_MARKER, raw, "旧版本被拒绝时不能夹带最新正文")
        self.assertNotIn("report", payload)

    def test_stale_version_and_revision_rejected_for_all_details(self):
        for path in DETAIL_PATHS:
            for name in ("version", "revision"):
                with self.subTest(path=path, parameter=name):
                    self.assert_error(path + "?" + name + "=1", 409,
                                      "STALE_MARKET_EVIDENCE")

    def test_exact_current_version_and_alias_return_the_requested_content(self):
        for path in DETAIL_PATHS:
            for suffix in ("?version=4", "?revision=4", "?version=4&revision=4"):
                with self.subTest(path=path, suffix=suffix):
                    status, payload, raw = self.request(path + suffix)
                    self.assertEqual(status, 200, payload)
                    obj = payload["report"] if "/weeklies/" in path else payload
                    self.assertEqual(obj["version"], CURRENT_VERSION)
                    self.assertEqual(obj["body"], CURRENT_MARKER)
                    self.assertNotIn("error", payload)

    def test_ambiguous_blank_conflicting_and_unsupported_queries_rejected(self):
        suffixes = (
            "?version=4&version=4", "?revision=4&revision=4",
            "?version=1&version=4", "?version=", "?revision=",
            "?version", "?version=4&revision=", "?version=4&revision=1",
            "?version=0", "?version=-1", "?version=04", "?version=1.0",
            "?version=true", "?version=1000000000", "?category=all",
            "?version=4&unexpected=value", "?unexpected=",
        )
        for path in DETAIL_PATHS:
            for suffix in suffixes:
                with self.subTest(path=path, suffix=suffix):
                    self.assert_error(path + suffix, 400, "MARKET_QUERY_INVALID")

    def test_unfiltered_current_details_and_latest_weekly_remain_readable(self):
        for path in (*DETAIL_PATHS, "/api/market/weeklies/latest"):
            with self.subTest(path=path):
                status, payload, _ = self.request(path)
                self.assertEqual(status, 200, payload)
                obj = payload["report"] if "/weeklies/" in path else payload
                self.assertEqual(obj["version"], CURRENT_VERSION)
                self.assertEqual(obj["body"], CURRENT_MARKER)

    def test_missing_current_weekly_cannot_satisfy_an_explicit_version(self):
        self.provider.weekly_missing = True
        try:
            for name in ("version", "revision"):
                with self.subTest(parameter=name):
                    self.assert_error(DETAIL_PATHS[0] + "?" + name + "=4", 409,
                                      "STALE_MARKET_EVIDENCE")
            status, payload, _ = self.request(DETAIL_PATHS[0])
            self.assertEqual(status, 200)
            self.assertIsNone(payload["report"])
        finally:
            self.provider.weekly_missing = False


if __name__ == "__main__":
    unittest.main()
