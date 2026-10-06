"""合成提供方仅验证公共 RAG 合同，不证明真实索引或金融资料已完成。"""
from copy import deepcopy
from datetime import date
import json
import unittest
from unittest.mock import patch

from agent_runtime.knowledge_retrieval import KnowledgeRetrieval, MAX_EXCERPT_CHARS


def evidence(citation_id="cite-1", **changes):
    quote = "赎回申请后预计两个工作日到账，具体以渠道确认条款为准。"
    row = {
        "schema_version": 1, "citation_id": citation_id, "release_id": "release-1",
        "document_id": "doc-1", "document_version_id": "version-1",
        "parse_id": "parse-1", "span_id": "span-1", "title": "合成产品说明书",
        "publisher": "合成管理人", "source_url": "https://example.com/terms.pdf",
        "excerpt": quote, "locator": {"page": 2, "paragraph": "赎回条款",
                                      "start_char": 100, "end_char": 100 + len(quote)},
        "product_id": "product-1", "share_class_id": "share-a", "channel_id": "channel-1",
        "published_on": "2026-09-01", "retrieved_on": "2026-10-01",
        "effective_from": "2026-09-01", "effective_to": "2026-11-01",
        "conditions": ["仅限该份额与该渠道"], "facets": ["liquidity", "terms"],
        "visibility": "public", "outbound_allowed": True, "withdrawn": False,
        "content_status": "full_text",
    }
    row.update(changes)
    return row


class Provider:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else {"cite-1": evidence()}
        self.release_id = "release-1"
        self.ready = True
        self.search_calls = []
        self.resolve_calls = []
        self.status_calls = 0
        self.status_error = self.search_error = self.resolve_error = None
        self.status_override = self.search_override = None
        self.gaps = []

    def status(self):
        self.status_calls += 1
        if self.status_error:
            raise self.status_error
        return self.status_override or {"schema_version": 1, "ready": self.ready,
                                       "provider_id": "local-test", "public_release_id": self.release_id}

    def search_public(self, request, release_id):
        self.search_calls.append((deepcopy(request), release_id))
        if self.search_error:
            raise self.search_error
        if self.search_override is not None:
            return deepcopy(self.search_override)
        return {"schema_version": 1, "release_id": release_id,
                "citation_ids": list(self.rows), "coverage_gaps": deepcopy(self.gaps)}

    def resolve_public_evidence(self, citation_id, release_id, as_of):
        self.resolve_calls.append((citation_id, release_id, as_of))
        if self.resolve_error:
            raise self.resolve_error
        return deepcopy(self.rows.get(citation_id))


def codes(result):
    return {gap["code"] for gap in result["coverage_gaps"]}


class KnowledgeRetrievalTest(unittest.TestCase):
    def search(self, provider, **args):
        return KnowledgeRetrieval(provider).search("查询赎回条款", as_of="2026-10-05", **args)

    def test_no_provider_is_unavailable_and_does_not_invent_hits(self):
        tool = KnowledgeRetrieval()
        self.assertFalse(tool.status()["ready"])
        self.assertFalse(tool.status()["private_retrieval_available"])
        result = tool.search("比较费用", as_of="2026-10-05")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["hits"], [])
        self.assertEqual(codes(result), {"provider_missing"})
        self.assertIsNone(result["public_release_id"])
        tool.assert_fresh()  # 未提供任何正文时，无需回查。

    def test_unready_and_bad_status_do_not_call_search(self):
        for override, expected in [
            ({"schema_version": 1, "ready": False, "provider_id": "local-test"}, "provider_not_ready"),
            ({"schema_version": 1, "ready": "true", "provider_id": "local-test",
              "public_release_id": "release-1"}, "invalid_provider_status"),
            ({"schema_version": True, "ready": True}, "invalid_provider_status"),
            ({"schema_version": 1, "ready": True, "provider_id": "local-test",
              "public_release_id": "../private"}, "invalid_provider_status"),
        ]:
            with self.subTest(expected=expected, override=override):
                provider = Provider()
                provider.status_override = override
                result = self.search(provider)
                self.assertIn(expected, codes(result))
                self.assertEqual(provider.search_calls, [])

    def test_valid_public_evidence_has_reproducible_citation_and_no_sale_inference(self):
        provider = Provider()
        result = self.search(provider, product_id="product-1", share_class_id="share-a",
                             channel_id="channel-1", facets=["liquidity"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["rejected_count"], 0)
        hit = result["hits"][0]
        self.assertEqual(hit["effective_status"], "within_documented_interval")
        self.assertEqual(hit["sale_status"], "unknown")
        self.assertFalse(hit["claims_are_account_facts"])
        self.assertEqual(hit["document_version_id"], "version-1")
        self.assertEqual(hit["parse_id"], "parse-1")
        self.assertEqual(hit["span_id"], "span-1")
        self.assertEqual(provider.search_calls[0][0]["scope"], "public")
        self.assertEqual(provider.resolve_calls, [("cite-1", "release-1", "2026-10-05")])

    def test_default_date_uses_real_market_date(self):
        provider = Provider()
        with patch("agent_runtime.knowledge_retrieval._today", return_value=date(2026, 10, 5)):
            result = KnowledgeRetrieval(provider).search("条款")
        self.assertEqual(result["as_of"], "2026-10-05")
        self.assertEqual(result["market_timezone"], "Asia/Shanghai")
        self.assertEqual(provider.search_calls[0][0]["as_of"], "2026-10-05")

    def test_private_forbidden_withdrawn_and_metadata_are_not_exported(self):
        cases = [({"visibility": "private"}, "private_evidence"),
                 ({"outbound_allowed": False}, "outbound_forbidden"),
                 ({"outbound_allowed": 1}, "outbound_forbidden"),
                 ({"withdrawn": True}, "withdrawn_evidence"),
                 ({"withdrawn": None}, "withdrawn_evidence"),
                 ({"content_status": "metadata_only"}, "metadata_only")]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                result = self.search(Provider({"cite-1": evidence(**changes)}))
                self.assertEqual(result["status"], "empty")
                self.assertEqual(result["hits"], [])
                self.assertIn(expected, codes(result))
                self.assertEqual(result["rejected_count"], 1)
                self.assertNotIn("赎回申请", json.dumps(result, ensure_ascii=False))

    def test_mismatched_version_citation_product_share_channel_are_rejected(self):
        for changes, args, expected in [
            ({"release_id": "release-2"}, {}, "release_mismatch"),
            ({"citation_id": "cite-other"}, {}, "citation_mismatch"),
            ({"product_id": "product-other"}, {"product_id": "product-1"}, "subject_mismatch"),
            ({"share_class_id": "share-c"}, {"share_class_id": "share-a"}, "subject_mismatch"),
            ({"channel_id": "channel-2"}, {"channel_id": "channel-1"}, "subject_mismatch"),
            ({"channel_id": None}, {"channel_id": "channel-1"}, "subject_mismatch"),
        ]:
            with self.subTest(changes=changes):
                result = self.search(Provider({"cite-1": evidence(**changes)}), **args)
                self.assertEqual(result["hits"], [])
                self.assertIn(expected, codes(result))

    def test_effective_interval_is_half_open_and_prevents_lookahead(self):
        for changes, expected in [
            ({"effective_from": "2026-10-06"}, "not_yet_effective"),
            ({"effective_to": "2026-10-05"}, "expired_evidence"),
            ({"effective_to": "2026-10-04"}, "expired_evidence"),
            ({"published_on": "2026-10-06"}, "published_after_as_of"),
            ({"effective_from": "2026-11-01", "effective_to": "2026-11-01"}, "invalid_evidence"),
            ({"effective_from": "2026-02-30"}, "invalid_evidence"),
        ]:
            with self.subTest(changes=changes):
                result = self.search(Provider({"cite-1": evidence(**changes)}))
                self.assertIn(expected, codes(result))
                self.assertFalse(result["hits"])
        result = self.search(Provider({"cite-1": evidence(effective_from="2026-10-05",
                                                          effective_to="2026-10-06")}))
        self.assertEqual(len(result["hits"]), 1)

    def test_missing_effective_bounds_are_unknown_instead_of_current_sale(self):
        for changes in ({"effective_to": None}, {"effective_from": None},
                        {"effective_from": None, "effective_to": None}):
            with self.subTest(changes=changes):
                result = self.search(Provider({"cite-1": evidence(**changes)}))
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["hits"][0]["effective_status"], "unknown")
                self.assertEqual(result["hits"][0]["sale_status"], "unknown")
                self.assertIn("effective_period_unknown", codes(result))
        result = self.search(Provider({"cite-1": evidence(published_on=None)}))
        self.assertIn("publication_date_unknown", codes(result))

    def test_locator_excerpt_and_source_are_strictly_validated(self):
        quote = "字" * (MAX_EXCERPT_CHARS + 1)
        cases = [
            {"excerpt": quote, "locator": {"page": 1, "paragraph": None,
                                           "start_char": 0, "end_char": len(quote)}},
            {"locator": {"page": None, "paragraph": None, "start_char": 100, "end_char": 128}},
            {"locator": {"page": True, "paragraph": None, "start_char": 100, "end_char": 128}},
            {"locator": {"page": 2, "paragraph": None, "start_char": 0, "end_char": 1}},
            {"source_url": "file:///Users/private/account.pdf"},
            {"source_url": "http://127.0.0.1/account"},
            {"source_url": "http://10.0.0.1/account"},
            {"source_url": "https://user:secret@example.com/terms"},
            {"source_url": "https://bank.local/terms"},
            {"document_version_id": "../private"},
            {"schema_version": True},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.search(Provider({"cite-1": evidence(**changes)}))
                self.assertFalse(result["hits"])
                self.assertIn("invalid_evidence", codes(result))

    def test_only_allowlisted_citation_fields_are_exported(self):
        row = evidence(raw_path="/Users/private", account_id="account-private",
                       customer_id="customer-private", raw_text="秘密原件", score=0.999,
                       arbitrary_payload={"token": "secret"})
        row["locator"]["raw_path"] = "/Users/private/paragraph"
        provider = Provider({"cite-1": row})
        provider.gaps = [{"code": "source_unavailable", "message": "/Users/private/customer"},
                         {"code": "secret-custom-code", "message": "秘密信息"}]
        result = self.search(provider)
        serialized = json.dumps(result, ensure_ascii=False)
        for marker in ("/Users/private", "account-private", "customer-private", "秘密", "score", "token"):
            self.assertNotIn(marker, serialized)
        self.assertIn("provider_reported_gap", codes(result))

    def test_missing_facets_and_empty_results_report_gaps(self):
        result = self.search(Provider(), facets=["liquidity", "fees"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual([g["facet"] for g in result["coverage_gaps"] if g["code"] == "facet_uncovered"], ["fees"])
        result = self.search(Provider({}), facets=["risk"])
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["hits"], [])
        self.assertIn("no_verified_evidence", codes(result))
        self.assertIn("facet_uncovered", codes(result))

    def test_candidates_are_bounded_before_resolve_and_duplicates_do_not_repeat_reads(self):
        provider = Provider({"cite-" + str(i): evidence("cite-" + str(i)) for i in range(10)})
        result = self.search(provider, limit=3)
        self.assertEqual(len(result["hits"]), 3)
        self.assertEqual(len(provider.resolve_calls), 3)
        self.assertEqual(provider.search_calls[0][0]["limit"], 3)
        self.assertEqual(result["rejected_count"], 7)
        self.assertIn("provider_limit_exceeded", codes(result))
        provider = Provider()
        provider.search_override = {"schema_version": 1, "release_id": "release-1",
                                    "citation_ids": ["cite-1", "cite-1", "../private"], "coverage_gaps": []}
        result = self.search(provider)
        self.assertEqual(len(provider.resolve_calls), 1)
        self.assertEqual(result["rejected_count"], 2)
        self.assertIn("duplicate_hit", codes(result))
        self.assertIn("invalid_citation", codes(result))

    def test_bad_requests_do_not_contact_provider(self):
        for args in ({"limit": 0}, {"limit": 9}, {"limit": True},
                     {"facets": ["unknown"]}, {"facets": [["fees"]]},
                     {"facets": ["fees"] * 9}, {"product_id": "../private"}):
            with self.subTest(args=args):
                provider = Provider()
                result = self.search(provider, **args)
                self.assertIn("invalid_request", codes(result))
                self.assertEqual(provider.status_calls, 0)
        for query, as_of in (("", "2026-10-05"), ("q" * 501, "2026-10-05"),
                             ("条款", "2026-02-30"), ("条款", "2026-1-01")):
            with self.subTest(query=query[:10], as_of=as_of):
                provider = Provider()
                result = KnowledgeRetrieval(provider).search(query, as_of=as_of)
                self.assertIn("invalid_request", codes(result))
                self.assertEqual(provider.status_calls, 0)

    def test_provider_errors_and_bad_search_schema_fail_safely(self):
        for field, expected in (("status_error", "provider_error"),
                                ("search_error", "provider_error"),
                                ("resolve_error", "resolver_error")):
            with self.subTest(field=field):
                provider = Provider()
                setattr(provider, field, RuntimeError("秘密路径 /Users/private"))
                result = self.search(provider)
                self.assertFalse(result["hits"])
                self.assertIn(expected, codes(result))
                self.assertNotIn("秘密", json.dumps(result, ensure_ascii=False))
        for response in ({"schema_version": 1, "release_id": "release-2", "citation_ids": [], "coverage_gaps": []},
                         {"schema_version": 1, "release_id": "release-1", "citation_ids": [], "coverage_gaps": "bad"},
                         {"schema_version": 1, "release_id": "release-1", "citation_ids": ["cite"] * 65, "coverage_gaps": []}):
            with self.subTest(response=response):
                provider = Provider()
                provider.search_override = response
                result = self.search(provider)
                self.assertIn("invalid_search_response", codes(result))
                self.assertFalse(result["ready"])
                self.assertFalse(provider.resolve_calls)

    def test_null_resolver_and_malformed_evidence_are_not_returned(self):
        for row, expected in ((None, "evidence_unavailable"),
                              ({}, "invalid_evidence"),
                              (evidence(facets=[["fees"]]), "invalid_evidence")):
            with self.subTest(row=row):
                result = self.search(Provider({"cite-1": row}))
                self.assertIn(expected, codes(result))
                self.assertFalse(result["hits"])

    def test_one_instance_pins_release_while_status_can_show_latest(self):
        provider = Provider()
        tool = KnowledgeRetrieval(provider)
        first = tool.search("条款", as_of="2026-10-05")
        provider.release_id = "release-2"
        self.assertEqual(tool.status()["public_release_id"], "release-2")
        second = tool.search("流动性", as_of="2026-10-05")
        self.assertEqual(first["public_release_id"], second["public_release_id"])
        self.assertEqual([r for _, r in provider.search_calls], ["release-1", "release-1"])
        self.assertTrue(all(r == "release-1" for _, r, _ in provider.resolve_calls))

    def test_freshness_rechecks_retraction_permission_version_and_content(self):
        changes = [{"withdrawn": True}, {"visibility": "private"}, {"outbound_allowed": False},
                   {"release_id": "release-2"}, {"document_version_id": "version-2"},
                   {"title": "修订版标题"}, {"product_id": "another-product"}]
        for change in changes:
            with self.subTest(change=change):
                provider = Provider()
                tool = KnowledgeRetrieval(provider)
                tool.search("条款", as_of="2026-10-05")
                provider.rows["cite-1"].update(change)
                with self.assertRaisesRegex(ValueError, "^stale_knowledge_evidence$"):
                    tool.assert_fresh()
        provider = Provider()
        tool = KnowledgeRetrieval(provider)
        tool.search("条款", as_of="2026-10-05")
        provider.resolve_error = RuntimeError("数据库异常")
        with self.assertRaisesRegex(ValueError, "^stale_knowledge_evidence$"):
            tool.search("复查", as_of="2026-10-05")

    def test_freshness_requires_provider_ready_and_same_identity(self):
        for mode in ("unready", "fault", "identity", "malformed"):
            with self.subTest(mode=mode):
                provider = Provider()
                tool = KnowledgeRetrieval(provider)
                tool.search("条款", as_of="2026-10-05")
                if mode == "unready":
                    provider.ready = False
                elif mode == "fault":
                    provider.status_error = RuntimeError("私有路径异常")
                elif mode == "identity":
                    provider.status_override = {"schema_version": 1, "ready": True,
                                                "provider_id": "other-provider",
                                                "public_release_id": "release-1"}
                else:
                    provider.status_override = {"schema_version": True, "ready": True}
                with self.assertRaisesRegex(ValueError, "^stale_knowledge_evidence$"):
                    tool.assert_fresh()

    def test_revision_inside_repeated_search_cannot_replace_delivered_snapshot(self):
        changes = [{"document_version_id": "version-2"}, {"parse_id": "parse-2"},
                   {"span_id": "span-2"}, {"title": "修订标题"},
                   {"channel_id": "channel-2"}, {"effective_to": "2026-12-01"},
                   {"conditions": ["修订条件"]},
                   {"locator": {"page": 3, "paragraph": "修订位置", "start_char": 100,
                                "end_char": 100 + len(evidence()["excerpt"])}}]
        for change in changes:
            with self.subTest(change=change):
                provider = Provider()
                tool = KnowledgeRetrieval(provider)
                first = tool.search("条款", as_of="2026-10-05")
                original_search = provider.search_public
                def revise(request, release_id):
                    provider.rows["cite-1"].update(change)
                    return original_search(request, release_id)
                provider.search_public = revise
                with self.assertRaisesRegex(ValueError, "^stale_knowledge_evidence$"):
                    tool.search("复查", as_of="2026-10-05")
                self.assertEqual(tool._delivered[("cite-1", "2026-10-05")][0], first["hits"][0])
                with self.assertRaisesRegex(ValueError, "^stale_knowledge_evidence$"):
                    tool.assert_fresh()

    def test_identical_repeated_citation_remains_usable(self):
        provider = Provider()
        tool = KnowledgeRetrieval(provider)
        first = tool.search("条款", as_of="2026-10-05", facets=["liquidity"])
        second = tool.search("复查", as_of="2026-10-05", product_id="product-1")
        self.assertEqual(first["hits"], second["hits"])
        self.assertEqual(len(tool._delivered), 1)
        tool.assert_fresh()

    def test_same_citation_at_different_dates_retains_both_snapshots(self):
        provider = Provider()
        original_resolve = provider.resolve_public_evidence
        def dated_resolve(citation_id, release_id, as_of):
            row = original_resolve(citation_id, release_id, as_of)
            row["document_version_id"] = "version-" + as_of
            return row
        provider.resolve_public_evidence = dated_resolve
        tool = KnowledgeRetrieval(provider)
        tool.search("条款", as_of="2026-10-05")
        tool.search("条款", as_of="2026-10-06")
        self.assertEqual(len(tool._delivered), 2)
        tool.assert_fresh()
        def revise_one_date(citation_id, release_id, as_of):
            row = dated_resolve(citation_id, release_id, as_of)
            if as_of == "2026-10-05": row["document_version_id"] = "reparsed-version"
            return row
        provider.resolve_public_evidence = revise_one_date
        with self.assertRaisesRegex(ValueError, "^stale_knowledge_evidence$"):
            tool.assert_fresh()

    def test_revision_during_multiple_resolves_is_detected_before_display(self):
        provider = Provider({"cite-1": evidence(), "cite-2": evidence("cite-2")})
        original_resolve = provider.resolve_public_evidence
        def revise_first(citation_id, release_id, as_of):
            if citation_id == "cite-2":
                provider.rows["cite-1"]["document_version_id"] = "version-2"
            return original_resolve(citation_id, release_id, as_of)
        provider.resolve_public_evidence = revise_first
        tool = KnowledgeRetrieval(provider)
        tool.search("条款", as_of="2026-10-05")
        with self.assertRaisesRegex(ValueError, "^stale_knowledge_evidence$"):
            tool.assert_fresh()

    def test_result_mutation_cannot_change_freshness_snapshot_and_scores_are_ignored(self):
        provider = Provider()
        tool = KnowledgeRetrieval(provider)
        result = tool.search("条款", as_of="2026-10-05")
        result["hits"][0]["title"] = "调用方改动"
        result["hits"][0]["locator"]["page"] = 99
        provider.rows["cite-1"]["score"] = 0.01
        tool.assert_fresh()
        self.assertEqual(provider.resolve_calls[-1], ("cite-1", "release-1", "2026-10-05"))


if __name__ == "__main__":
    unittest.main()
