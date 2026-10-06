"""公共交接独立验收：原件/SQL预期先冻结，全部变更仅在临时副本。

python3 -B -m unittest discover -s tests/window_integration -p test_public_handoff.py -v
五项通过只说明本地提供/回查合同；不等于当前在售或Agent最终语义通过。
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
ORACLE_PATH = ROOT / "evaluation/window_integration/public_handoff_oracles.json"
FROZEN_ORACLE_SHA256 = "b15ae0ebc1ef8dad98ae6ab27cbcad04fbb145e45fb3a91d87666aa74d10ea02"


def sha(value):
    return hashlib.sha256(value).hexdigest()


def file_sha(path):
    return sha(path.read_bytes())


def read_only(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return closing(db)


class IndependentPublicHandoff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if file_sha(ORACLE_PATH) != FROZEN_ORACLE_SHA256:
            raise RuntimeError("frozen_public_oracle_changed")
        cls.oracle = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
        if not cls.oracle["expectations_frozen_before_execution"] or len(cls.oracle["cases"]) != 5:
            raise RuntimeError("invalid_frozen_public_oracle")
        cls.data_root = ROOT / cls.oracle["data_root"]
        cls.source_data = {name: file_sha(cls.data_root / name) for name in cls.oracle["data_sha256"]}
        if cls.source_data != cls.oracle["data_sha256"]:
            raise RuntimeError("public_handoff_data_hash_mismatch: 不回落现役或新版数据")
        cls.source_code = {name: file_sha(ROOT / name) for name in cls.oracle["source_baseline_sha256"]}
        if any(name.startswith("local_knowledge") or name in ("market_content", "market_daily", "market_sources") for name in sys.modules):
            raise RuntimeError("fresh_process_required: 请单独运行本套复现命令")
        sys.path.insert(0, str(ROOT))
        cls.addClassCleanup(lambda: sys.path.remove(str(ROOT)))
        # 保留socket类型，拦截真正出站与DNS；不加载任何密钥/服务。
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                       "socket.getaddrinfo", "urllib.request.urlopen", "urllib.request.OpenerDirector.open"):
            guard = patch(target, side_effect=AssertionError("network_forbidden_in_public_handoff"))
            guard.start()
            cls.addClassCleanup(guard.stop)
        cls.local = importlib.import_module("local_knowledge.store")
        cls.market_module = importlib.import_module("market_content")
        cls.daily = importlib.import_module("market_daily")
        # 仅加载公共消费合同，避免导入Agent编排或模型配置。
        spec = importlib.util.spec_from_file_location("window_public_knowledge_consumer", ROOT / "agent_runtime/knowledge_retrieval.py")
        cls.consumer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.consumer)
        if cls.source_code != {name: file_sha(ROOT / name) for name in cls.source_code}:
            raise RuntimeError("source_changed_during_public_import")
        cls.now = datetime.fromisoformat(cls.oracle["market_now"])

    @classmethod
    def tearDownClass(cls):
        if cls.source_data != {name: file_sha(cls.data_root / name) for name in cls.source_data}:
            raise RuntimeError("public_source_artifact_changed_or_written: 本轮结果无效")
        if cls.source_code != {name: file_sha(ROOT / name) for name in cls.source_code} or file_sha(ORACLE_PATH) != FROZEN_ORACLE_SHA256:
            raise RuntimeError("public_candidate_or_oracle_changed: 本轮结果无效")
        print(json.dumps({"suite": cls.oracle["suite_id"], "snapshot_verification_only": True,
                          "oracle_sha256": FROZEN_ORACLE_SHA256, "data_sha256": cls.source_data,
                          "candidate_sha256": cls.source_code,
                          "candidate_matches_authoring_baseline": cls.source_code == cls.oracle["source_baseline_sha256"],
                          "source_artifacts_unchanged": True, "network_calls_allowed": False,
                          "cloud_model_calls": 0, "formal_semantic_acceptance": "not_run"},
                         ensure_ascii=False, sort_keys=True))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="navren-public-integration-")
        self.addCleanup(self.temp.cleanup)
        self.temp_root = Path(self.temp.name)
        self.knowledge_path = self.copy_db("reviewed-public.sqlite3", "knowledge.sqlite3")
        self.market_path = self.copy_db("frozen-market.sqlite3", "market.sqlite3")

    def copy_db(self, source, name):
        target = self.temp_root / name
        shutil.copyfile(self.data_root / source, target)
        self.assertEqual(file_sha(target), self.oracle["data_sha256"][source])
        return target

    def provider(self, path=None):
        return self.local.LocalKnowledgeProvider(path or self.knowledge_path, release_id=self.oracle["release_id"])

    def fee_search(self, consumer, evidence=None, **changes):
        evidence = evidence or self.oracle["fee_evidence"][0]
        params = {"query": "申购费 赎回费", "product_id": evidence["product_id"],
                  "share_class_id": evidence["share_class_id"], "channel_id": evidence["channel_id"],
                  "as_of": self.oracle["as_of"], "facets": ["fees"], "limit": 8}
        params.update(changes)
        return consumer.search(**params)

    def assert_fee_hit(self, hit, expected):
        for key in ("citation_id", "document_id", "product_id", "share_class_id", "channel_id", "source_url", "excerpt"):
            self.assertEqual(hit[key], expected[key])
        self.assertEqual(hit["document_version_id"], expected["version_id"])
        self.assertEqual(hit["release_id"], self.oracle["release_id"])
        for key in ("page", "start_char", "end_char"):
            self.assertEqual(hit["locator"][key], expected[key])
        self.assertEqual(sha(hit["excerpt"].encode()), expected["excerpt_sha256"])
        for key in ("sale_status", "effective_status", "effective_from", "effective_to"):
            self.assertEqual(hit[key], self.oracle["public_catalog"][key])
        self.assertFalse(hit["claims_are_account_facts"])

    def test_PH01_source_hash_public_identity_and_fixed_release(self):
        expected = self.oracle["public_catalog"]
        manifest = json.loads((self.data_root / "integration-data-manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["public_only"])
        self.assertTrue(manifest["source_read_only"])
        self.assertEqual(manifest["knowledge_database"]["release_id"], self.oracle["release_id"])
        self.assertEqual(manifest["knowledge_database"]["sha256"], self.oracle["data_sha256"]["reviewed-public.sqlite3"])
        with read_only(self.data_root / "reviewed-public.sqlite3") as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse(tables.intersection(expected["excluded_private_tables"]))
            for table, key in (("products", "product_count"), ("share_classes", "share_count"), ("chunks", "chunk_count"),
                               ("document_versions", "document_version_count"), ("channels", "channel_count")):
                self.assertEqual(db.execute("SELECT count(*) FROM " + table).fetchone()[0], expected[key])
            self.assertEqual(dict(db.execute("SELECT id,product_id FROM share_classes")), expected["share_to_product"])
            for row in db.execute("SELECT visibility,data_origin FROM documents"):
                self.assertEqual(tuple(row), ("public", "real"))
            for row in db.execute("SELECT effective_from,effective_to FROM document_versions"):
                self.assertEqual(tuple(row), (None, None))
        provider = self.provider()
        status = provider.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["public_release_id"], self.oracle["release_id"])
        self.assertEqual(status["published_chunk_count"], expected["chunk_count"])
        self.assertEqual(status["private_retrieval_available"], expected["private_retrieval_available"])
        retrieval = self.consumer.KnowledgeRetrieval(provider)
        self.assertEqual(len(self.fee_search(retrieval)["hits"]), 1)
        # 只在临时副本发布已有文档子集；本轮旧提供器/消费仍固定原发布。
        other_release = provider.publish(version_ids=[self.oracle["fee_evidence"][0]["version_id"]], activate=True)
        self.assertNotEqual(other_release, self.oracle["release_id"])
        self.assertEqual(provider.status()["public_release_id"], self.oracle["release_id"])
        self.assertEqual(self.fee_search(retrieval)["public_release_id"], self.oracle["release_id"])
        retrieval.assert_fresh()

    def test_PH02_original_fee_spans_share_and_channel_do_not_cross(self):
        provider = self.provider()
        for expected in self.oracle["fee_evidence"]:
            with self.subTest(share=expected["share_class_id"]):
                # 独立核对原件完整字节、解析正文及引用跨度，不用搜索结果生成黄金预期。
                with read_only(self.data_root / "reviewed-public.sqlite3") as db:
                    row = db.execute("SELECT c.*,v.parsed_text,v.parsed_sha256,v.raw_sha256,a.payload FROM chunks c JOIN document_versions v ON v.id=c.version_id JOIN raw_artifacts a ON a.sha256=v.raw_sha256 WHERE c.id=?", (expected["citation_id"],)).fetchone()
                    self.assertEqual(sha(bytes(row["payload"])), expected["raw_sha256"])
                    self.assertTrue(bytes(row["payload"]).startswith(b"%PDF-"))
                    self.assertEqual(sha(row["parsed_text"].encode()), expected["parsed_sha256"])
                    self.assertEqual(row["parsed_text"][expected["start_char"]:expected["end_char"]], expected["excerpt"])
                    self.assertEqual(row["excerpt"], expected["excerpt"])
                result = self.fee_search(self.consumer.KnowledgeRetrieval(provider), expected)
                self.assertEqual(len(result["hits"]), 1)
                self.assert_fee_hit(result["hits"][0], expected)
                self.assertIn("effective_period_unknown", {gap["code"] for gap in result["coverage_gaps"]})
        for probe in self.oracle["missing_identity_probes"]:
            with self.subTest(identity=probe):
                result = self.fee_search(self.consumer.KnowledgeRetrieval(provider), **{key: probe[key] for key in ("product_id", "share_class_id", "channel_id")})
                self.assertEqual(result["hits"], [])
                self.assertIn(probe["gap"], {gap["code"] for gap in result["coverage_gaps"]})
        sale = self.fee_search(self.consumer.KnowledgeRetrieval(provider), facets=["sale_status"])
        self.assertEqual(sale["hits"], [])
        self.assertIn("sale_status_unknown", {gap["code"] for gap in sale["coverage_gaps"]})

    def test_PH03_withdraw_revision_and_freshness_block_delivered_quotes(self):
        evidence = self.oracle["fee_evidence"][0]
        for kind, expected in self.oracle["lifecycle_probes"].items():
            with self.subTest(lifecycle=kind):
                path = self.copy_db("reviewed-public.sqlite3", kind + ".sqlite3")
                provider = self.provider(path)
                consumer = self.consumer.KnowledgeRetrieval(provider)
                self.assertEqual(len(self.fee_search(consumer)["hits"]), 1)
                if kind == "withdrawal":
                    provider.withdraw(evidence["version_id"])
                else:
                    with provider.db() as db:
                        if kind == "revision_cutoff":
                            # 模拟受信新版本确认后的旧版本截止门；不声称插入了真实新原件。
                            db.execute("UPDATE document_versions SET revoked_from=? WHERE id=?", (expected["revoked_from"], evidence["version_id"]))
                        else:
                            rules = json.loads(db.execute("SELECT freshness FROM sources WHERE id='orient-reviewed-pdf'").fetchone()[0])
                            rules["fees"] = expected["maximum_age_days"]
                            db.execute("UPDATE sources SET freshness=? WHERE id='orient-reviewed-pdf'", (json.dumps(rules),))
                self.assertIs(provider.resolve_public_evidence(evidence["citation_id"], self.oracle["release_id"], self.oracle["as_of"]), expected["canonical"])
                with self.assertRaisesRegex(ValueError, expected["consumer_error"]):
                    consumer.assert_fresh()
                result = self.fee_search(self.consumer.KnowledgeRetrieval(provider))
                self.assertEqual(result["hits"], [])
                if "query_gap" in expected:
                    self.assertIn(expected["query_gap"], {gap["code"] for gap in result["coverage_gaps"]})

    def test_PH04_history_missing_null_weekly_and_nav_boundaries(self):
        expected = self.oracle["market_history"]
        with read_only(self.data_root / "frozen-market.sqlite3") as db:
            editions = [dict(row) for row in db.execute("SELECT edition_date,revision,origin,period_state,item_count FROM market_daily_editions ORDER BY edition_date")]
            self.assertEqual(editions, expected["editions"])
            members = [list(row) for row in db.execute("SELECT edition_date,revision,position,event_id,event_version FROM market_daily_items ORDER BY edition_date,position")]
            self.assertEqual(members, expected["membership"])
        market = self.market_module.MarketStore(self.market_path)
        index = market.history_index(expected["window_start"], expected["window_end"], now=self.now)
        self.assertEqual(len(index["dates"]), expected["window_days"])
        missing = [row for row in index["dates"] if row["archive_state"] == "missing"]
        present = [row for row in index["dates"] if row["archive_state"] != "missing"]
        self.assertEqual(len(missing), expected["missing_days"])
        self.assertEqual(len(present), expected["archived_days"])
        self.assertEqual(sum(row["total_count"] for row in present), expected["members"])
        for row in missing:
            for key in ("total_count", "matched_count", "readable_count"):
                self.assertIs(row[key], expected["missing_archive_counts"])
        day = market.history_day(missing[0]["date"], now=self.now)
        self.assertEqual(day["status"], "missing_archive")
        for key in ("total_count", "matched_count", "original_count", "readable_count"):
            self.assertIs(day[key], expected["missing_archive_counts"])
        for day, revision, position, event_id, event_version in expected["membership"]:
            member = market.history_event(day, event_id, revision, now=self.now, expected_version=event_version)
            self.assertEqual(member["archive"]["event_version"], event_version)
            self.assertEqual(member["archive"]["position"], position)
            self.assertEqual(member["availability"], "active")
        for period, weekly in expected["latest_weeklies"].items():
            result = market.weekly(period)
            self.assertEqual(result["report"]["version"], weekly["version"])
            self.assertEqual(result["report"]["status"], weekly["status"])
            self.assertNotEqual(result["report"]["status"], "published")
        self.assertIs(market.weekly("latest")["report"], expected["latest_active_weekly"])
        nav = json.loads((self.data_root / "sources/nav/nav-delivery.json").read_text(encoding="utf-8"))
        boundary = self.oracle["nav_boundary"]
        for key in ("products", "nav_rows", "nav_original_pages", "interval_statements", "full_window_reinvested_total_return_ready"):
            self.assertEqual(nav["summary"][key], boundary[key])
        self.assertEqual(sorted(product["share_code"] for product in nav["products"]), boundary["share_codes"])
        self.assertNotIn(boundary["missing_nav_share"], {product["share_code"] for product in nav["products"]})
        for product in nav["products"]:
            self.assertEqual(len(product["navs"]), boundary["rows_per_share"])
            self.assertEqual(product["navs"][0]["nav_on"], boundary["first_nav_on"])
            self.assertEqual(product["navs"][-1]["nav_on"], boundary["last_nav_on"])

    def test_PH05_historical_identity_revision_and_withdrawal_clear_old_facts(self):
        expected = self.oracle["market_history"]["daily_event_probe"]
        market = self.market_module.MarketStore(self.market_path)
        params = (expected["edition_date"], expected["event_id"], expected["revision"])
        baseline = market.history_event(*params, now=self.now, expected_version=expected["event_version"])
        self.assertEqual(baseline["availability"], expected["baseline_availability"])
        self.assertEqual(baseline["archive"]["article_versions"], expected["article_versions"])
        for kind, event_id, revision, version in (
            ("wrong_event_version", expected["event_id"], 1, 2),
            ("nonmember", expected["nonmember_id"], 1, None),
            ("missing_revision", expected["event_id"], 2, None),
        ):
            with self.subTest(rejection=kind), self.assertRaises(self.daily.MarketDailyError) as error:
                market.history_event(expected["edition_date"], event_id, revision, now=self.now, expected_version=version)
            self.assertEqual(error.exception.code, expected[kind + "_error"])
            self.assertEqual(error.exception.status, expected[kind + "_http_status"])
        with market.db() as db:
            article = json.loads(db.execute("SELECT body FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1", (expected["primary_article_id"],)).fetchone()[0])
        article["evidence_text"] += "\n集成临时副本修订探针。"
        article["fingerprint"] = sha(article["evidence_text"].encode())
        article["checked_at"] = self.oracle["market_now"]
        self.assertTrue(market.save_article(article))
        revised = market.history_event(*params, now=self.now, expected_version=expected["event_version"])
        self.assertEqual(revised["availability"], expected["after_source_revision"])
        market.withdraw_article(expected["primary_article_id"])
        withdrawn = market.history_event(*params, now=self.now, expected_version=expected["event_version"])
        self.assertEqual(withdrawn["availability"], expected["after_withdrawal"])
        for result in (revised, withdrawn):
            self.assertEqual(result["summary"], expected["invalidated_summary"])
            self.assertEqual(result["facts"], expected["invalidated_facts"])
            self.assertEqual(result["archive"]["article_versions"], expected["article_versions"])
            for article in result["articles"]:
                self.assertNotIn("evidence_excerpt", article)
        with market.db() as db:
            snapshot = db.execute("SELECT snapshot FROM market_daily_items WHERE edition_date=? AND revision=? AND event_id=?",
                                  (expected["edition_date"], expected["revision"], expected["event_id"])).fetchone()[0]
        self.assertEqual(sha(snapshot.encode()), expected["snapshot_sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
