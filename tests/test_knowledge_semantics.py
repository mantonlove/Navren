"""真实纯函数的合成安全回归；不使用个人资料、冻结题或云端模型。"""
from copy import deepcopy
import json

from functools import partial, update_wrapper
import unittest


def cases(name, values):
    """用 unittest 原生 FunctionTestCase 展开互相独立的安全反例。"""
    def decorate(function):
        function.test_cases = (name, values)
        return function
    return decorate

from agent_runtime.knowledge_semantics import (
    PROJECTION_POLICY, SEMANTIC_FIELDS, SEMANTIC_GAP_CODES, SEMANTIC_GAP_MESSAGES,
    SEMANTIC_ROLES, boundary_projection, consumer_projection, unique_semantic_gaps,
)
from local_knowledge.field_semantics import FIELD_REGISTRY, ROLE_PHRASES, SEMANTIC_POLICY
from webank_catalog import URL as WEBANK_DISCLOSURE_URL


RELEASE = "release.synthetic"
A, B = "WEBANK:SYNTHETIC-A", "WEBANK:SYNTHETIC-B"


def hit(field="publish", value="合成基金管理公司", *, product=A,
        citation="citation.synthetic", version="version.synthetic", **changes):
    excerpt = json.dumps(field, ensure_ascii=False) + ":" + json.dumps(value, ensure_ascii=False)
    result = {
        "citation_id": citation, "release_id": RELEASE,
        "document_id": "document.synthetic", "document_version_id": version,
        "parse_id": "parse.synthetic", "span_id": "span.synthetic",
        "visibility": "public", "outbound_allowed": True, "content_status": "full_text",
        "product_id": product, "share_class_id": None, "channel_id": "WEBANK",
        "source_url": WEBANK_DISCLOSURE_URL, "excerpt": excerpt,
        "locator": {"page": None, "paragraph": "productPresentation:" + product[7:] + "/" + field,
                    "start_char": 12, "end_char": 12 + len(excerpt)},
    }
    result.update(changes)
    return result


def contract(*, objects=(A,), fields=("publish",), roles=("issuer",), citations=("citation.synthetic",),
             references=(), status="supported", gaps=()):
    return {
        "schema_version": 1, "policy": SEMANTIC_POLICY,
        "objects": list(objects), "requested_fields": list(fields), "requested_roles": list(roles),
        "unsupported_roles": [role for role in roles if role != "issuer"],
        "field_support": [{"product_id": product, "field": field, "status": status,
                           "citation_ids": list(citations), "reference_citation_ids": list(references)}
                          for product in objects for field in fields],
        "supported_roles": [{"product_id": product, "role": role, "original_field": "publish",
                             "citation_ids": list(citations), "support_scope": "source_disclosed_role_only"}
                            for product in objects for role in roles],
        "coverage_gaps": list(gaps),
    }


def project(metadata, hits=(), *, gaps=(), request=None, trusted=()):
    return consumer_projection({"retrieval": {"semantic_coverage": metadata}, "coverage_gaps": list(gaps)},
                               list(hits), RELEASE, request=request, trusted_public_product_ids=trusted)


def codes(result):
    return {gap["code"] for gap in result["coverage_gaps"]}


def test_finite_enums_reuse_registered_meaning():
    assert SEMANTIC_FIELDS == frozenset(FIELD_REGISTRY)
    assert SEMANTIC_ROLES == frozenset(ROLE_PHRASES)
    assert "context_incomplete" in SEMANTIC_GAP_CODES


def test_successful_original_issuer_and_field_support_contains_only_verified_refs():
    result = project(contract(), [hit()])
    coverage = result["semantic_coverage"]
    assert result["coverage_gaps"] == []
    assert coverage["projection_policy"] == PROJECTION_POLICY
    assert coverage["supported_roles"] == [{
        "product_id": A, "role": "issuer", "original_field": "publish",
        "citation_ids": ["citation.synthetic"], "document_version_id": "version.synthetic",
        "release_id": RELEASE, "support_scope": "source_disclosed_role_only",
    }]
    assert coverage["field_support"][0]["status"] == "supported"
    assert "合成基金管理公司" not in json.dumps(result, ensure_ascii=False)
    assert "not_final_answer_validation" in coverage["scope"]


@cases("role", ["manager", "sales_institution", "custodian"])
def test_issuer_company_name_never_supports_other_roles(role):
    result = project(contract(fields=(), roles=(role,)), [hit()])
    coverage = result["semantic_coverage"]
    assert coverage["supported_roles"] == []
    assert coverage["unsupported_roles"] == [role]
    assert result["coverage_gaps"] == [{
        "code": "unsupported_semantic_role", "message": SEMANTIC_GAP_MESSAGES["unsupported_semantic_role"],
        "product_id": A, "role": role,
    }]


def test_management_fee_support_cannot_be_a_manager_identity():
    metadata = contract(fields=("charge",), roles=("manager",))
    result = project(metadata, [hit("charge", "管理费为合成标准")])
    assert result["semantic_coverage"]["field_support"][0]["status"] == "supported"
    assert result["semantic_coverage"]["supported_roles"] == []
    assert "unsupported_semantic_role" in codes(result)


def test_each_product_keeps_its_own_coverage_gap():
    metadata = contract(objects=(A, B))
    result = project(metadata, [hit(), hit("type", "合成类别", product=B, citation="citation.b")])
    coverage = result["semantic_coverage"]
    assert [entry["product_id"] for entry in coverage["supported_roles"]] == [A]
    assert [(item["product_id"], item["status"]) for item in coverage["field_support"]] == [
        (A, "supported"), (B, "uncovered"),
    ]
    assert {gap["product_id"] for gap in result["coverage_gaps"]} == {B}


def test_two_manager_gaps_are_not_collapsed_to_one():
    result = project(contract(objects=(A, B), fields=(), roles=("manager",)),
                     [hit(), hit(product=B, citation="citation.b")])
    assert len(result["coverage_gaps"]) == 2
    assert {gap["product_id"] for gap in result["coverage_gaps"]} == {A, B}


def test_request_id_shape_alone_never_proves_public_identity():
    result = project(contract(fields=(), roles=("manager",)), [], request={"product_id": A})
    assert result["semantic_coverage"]["objects"] == []
    assert codes(result) == {"provider_reported_gap"}
    assert A not in json.dumps(result)


def test_explicit_trusted_public_request_preserves_no_hit_manager_gap():
    result = project(contract(fields=(), roles=("manager",)), [],
                     request={"product_id": A}, trusted={A})
    assert result["semantic_coverage"]["objects"] == [A]
    assert result["coverage_gaps"][0]["product_id"] == A
    assert codes(result) == {"unsupported_semantic_role"}


def test_catalogue_membership_cannot_authorize_another_requested_identity():
    result = project(contract(objects=(B,), fields=(), roles=("manager",)), [],
                     request={"product_id": A}, trusted={A, B})
    assert result["semantic_coverage"]["objects"] == []
    assert B not in json.dumps(result)


@cases("changes", [
    {"withdrawn": True}, {"visibility": "private"}, {"outbound_allowed": False},
    {"content_status": "metadata_only"}, {"release_id": "release.other"},
    {"product_id": B}, {"document_version_id": None},
])
def test_rejected_success_set_member_downgrades_declared_support(changes):
    result = project(contract(), [hit(**changes)], request={"product_id": A}, trusted={A})
    assert result["semantic_coverage"]["supported_roles"] == []
    assert result["semantic_coverage"]["field_support"][0]["status"] == "uncovered"
    assert "semantic_support_unavailable" in codes(result)


def test_resolver_removed_citation_loses_positive_status():
    result = project(contract(), [], request={"product_id": A}, trusted={A})
    assert result["semantic_coverage"]["field_support"][0]["citation_ids"] == []
    assert result["semantic_coverage"]["supported_roles"] == []
    assert "semantic_support_unavailable" in codes(result)


@cases("mutation", [
    "source", "field", "paragraph", "offset", "multiple_keys", "partial", "noncanonical",
    "empty_value", "page", "share", "channel",
])
def test_unregistered_or_non_original_field_shape_never_supports_issuer(mutation):
    evidence = hit()
    if mutation == "source":
        evidence["source_url"] = "https://unregistered.example.test/public"
    elif mutation == "field":
        evidence = hit("type", "合成类别")
    elif mutation == "paragraph":
        evidence["locator"]["paragraph"] = "productPresentation:SYNTHETIC-A/charge"
    elif mutation == "offset":
        evidence["locator"]["end_char"] += 1
    elif mutation == "multiple_keys":
        evidence["excerpt"] += ',"type":"合成类别"'
    elif mutation == "partial":
        evidence["excerpt"] = '"publish":"合成'
    elif mutation == "noncanonical":
        evidence["excerpt"] = '"publish": "合成基金管理公司"'
    elif mutation == "empty_value":
        evidence = hit(value=" ")
    elif mutation == "page":
        evidence["locator"]["page"] = 1
    elif mutation == "share":
        evidence["share_class_id"] = "share.synthetic"
    elif mutation == "channel":
        evidence["channel_id"] = "OTHER"
    result = project(contract(), [evidence])
    assert result["semantic_coverage"]["supported_roles"] == []
    assert "semantic_support_unavailable" in codes(result)


def test_positive_group_cannot_mix_original_versions():
    metadata = contract(citations=("citation.a", "citation.b"))
    result = project(metadata, [hit(citation="citation.a"), hit(citation="citation.b", version="version.other")])
    assert result["semantic_coverage"]["supported_roles"] == []
    assert result["semantic_coverage"]["field_support"][0]["status"] == "uncovered"


def test_positive_group_cannot_mix_documents_with_a_reused_version():
    metadata = contract(citations=("citation.a", "citation.b"))
    result = project(metadata, [hit(citation="citation.a"), hit(citation="citation.b", document_id="document.other")])
    assert result["semantic_coverage"]["supported_roles"] == []


def test_any_missing_claimed_citation_downgrades_entire_group():
    result = project(contract(citations=("citation.synthetic", "citation.missing")), [hit()])
    assert result["semantic_coverage"]["supported_roles"] == []
    assert result["semantic_coverage"]["field_support"][0]["citation_ids"] == []


def test_duplicate_verified_citation_fails_closed_even_if_one_is_invalid():
    result = project(contract(), [hit(visibility="private"), hit()], request={"product_id": A}, trusted={A})
    assert result["semantic_coverage"]["supported_roles"] == []


def test_reference_only_field_cannot_support_a_role_or_disclosed_value():
    metadata = contract(citations=(), references=("citation.synthetic",), status="reference_only")
    result = project(metadata, [hit(value="详见产品合同")])
    assert result["semantic_coverage"]["field_support"][0]["status"] == "reference_only"
    assert result["semantic_coverage"]["supported_roles"] == []
    assert "semantic_field_reference_only" in codes(result)


def test_reference_marked_as_concrete_disclosure_is_downgraded():
    result = project(contract(), [hit(value="详见产品合同")])
    assert result["semantic_coverage"]["field_support"][0]["status"] == "uncovered"
    assert result["semantic_coverage"]["supported_roles"] == []


def test_no_provider_text_query_path_or_unverified_object_crosses_projection():
    sentinel = "PRIVATE_SENTINEL_ACCOUNT_PATH_QUERY"
    metadata = contract(fields=(), roles=("manager",), gaps=({
        "code": "unsupported_semantic_role", "product_id": A, "role": "manager",
        "message": sentinel, "reason": sentinel, "query": sentinel, "path": sentinel,
    },))
    metadata.update(query=sentinel, private_context=sentinel)
    metadata["supported_roles"][0].update(message=sentinel, value=sentinel)
    raw = {"retrieval": {"semantic_coverage": metadata, "query": sentinel, "ranking": sentinel},
           "coverage_gaps": [{"code": "context_incomplete", "message": sentinel, "product_id": sentinel,
                              "role": sentinel, "reason": sentinel},
                             {"code": "unsupported_semantic_role", "product_id": sentinel, "role": "manager"}]}
    result = consumer_projection(raw, [hit()], RELEASE)
    serialized = json.dumps(result, ensure_ascii=False)
    assert sentinel not in serialized
    assert "context_incomplete" in codes(result)
    assert "provider_reported_gap" in codes(result)


def test_context_incomplete_survives_without_coverage_metadata_or_any_hits():
    result = consumer_projection({"coverage_gaps": [{"code": "context_incomplete", "message": "任意文本"}]}, [], RELEASE)
    assert result == {"coverage_gaps": [{"code": "context_incomplete",
                                         "message": SEMANTIC_GAP_MESSAGES["context_incomplete"]}],
                      "semantic_coverage": None}


def test_plain_old_provider_without_semantic_metadata_remains_compatible():
    raw = {"citation_ids": [], "coverage_gaps": [{"code": "facet_uncovered", "facet": "fees"}]}
    assert consumer_projection(raw, [], RELEASE) == {"coverage_gaps": [], "semantic_coverage": None}


def test_registered_issuer_is_missing_instead_of_an_unsupported_role():
    result = project(contract(fields=(), roles=("issuer",), citations=()), [hit("type", "合成类别")],
                     gaps=({"code": "unsupported_semantic_role", "product_id": A, "role": "issuer"},))
    assert "unsupported_semantic_role" not in codes(result)
    assert "semantic_role_uncovered" in codes(result)


@cases("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"policy": "unregistered"},
    {"requested_roles": ["PRIVATE_UNKNOWN_ROLE"]}, {"requested_fields": ["PRIVATE_UNKNOWN_FIELD"]},
    {"objects": ["PRIVATE_ID/with/path"]}, {"field_support": "PRIVATE_STRING"},
    {"supported_roles": [None]}, {"coverage_gaps": [None]},
    {"objects": [A] * 9},
])
def test_malformed_contract_loses_all_positive_claims(changes):
    metadata = contract()
    metadata.update(changes)
    result = project(metadata, [hit()])
    assert result["semantic_coverage"] is None
    assert codes(result) == {"semantic_contract_invalid"}
    assert "PRIVATE" not in json.dumps(result)


def test_oversized_gap_contract_is_not_silently_truncated_to_positive_support():
    products = tuple("WEBANK:SYNTHETIC-" + str(i) for i in range(8))
    metadata = contract(objects=products, fields=("risk", "placement", "type", "charge", "chargeWay"), roles=(),
                        status="uncovered", citations=())
    result = project(metadata, [hit(product=product, citation="citation." + str(i))
                                for i, product in enumerate(products)])
    assert result["semantic_coverage"] is None
    assert codes(result) == {"semantic_contract_invalid"}


def test_duplicate_positive_records_never_select_one_arbitrarily():
    metadata = contract()
    metadata["supported_roles"].append(deepcopy(metadata["supported_roles"][0]))
    metadata["field_support"].append(deepcopy(metadata["field_support"][0]))
    result = project(metadata, [hit()])
    assert result["semantic_coverage"]["supported_roles"] == []
    assert result["semantic_coverage"]["field_support"][0]["status"] == "uncovered"


def test_boundary_projection_revalidates_and_is_idempotent():
    evidence = hit()
    first = project(contract(), [evidence])
    data = {**first, "release_id": RELEASE, "hits": [evidence], "private_extra": "PRIVATE_SENTINEL"}
    second = boundary_projection(data)
    assert second == first
    assert "PRIVATE_SENTINEL" not in json.dumps(second)
    third = boundary_projection({**second, "release_id": RELEASE, "hits": [evidence]})
    assert third == second


def test_boundary_uses_real_consumer_public_release_id_shape():
    evidence = hit()
    first = project(contract(), [evidence])
    second = boundary_projection({**first, "public_release_id": RELEASE, "hits": [evidence]})
    assert second == first


def test_boundary_rejects_conflicting_top_level_release_identifiers():
    first = project(contract(), [hit()])
    result = boundary_projection({**first, "public_release_id": RELEASE,
                                  "release_id": "release.other", "hits": [hit()]})
    assert result["semantic_coverage"] is None
    assert codes(result) == {"semantic_contract_invalid"}


def test_boundary_never_rebinds_projected_support_to_a_changed_original_version():
    first = project(contract(), [hit()])
    result = boundary_projection({**first, "public_release_id": RELEASE,
                                  "hits": [hit(version="version.changed")]})
    assert result["semantic_coverage"]["supported_roles"] == []
    assert result["semantic_coverage"]["field_support"][0]["status"] == "uncovered"
    assert "semantic_support_unavailable" in codes(result)


def test_boundary_requires_positive_record_version_and_release_bindings():
    first = project(contract(), [hit()])
    for item in first["semantic_coverage"]["field_support"] + first["semantic_coverage"]["supported_roles"]:
        item.pop("document_version_id")
        item.pop("release_id")
    result = boundary_projection({**first, "public_release_id": RELEASE, "hits": [hit()]})
    assert result["semantic_coverage"]["supported_roles"] == []
    assert result["semantic_coverage"]["field_support"][0]["status"] == "uncovered"


def test_consumer_honors_optional_original_support_version_binding():
    metadata = contract()
    for item in metadata["field_support"] + metadata["supported_roles"]:
        item.update(document_version_id="version.other", release_id=RELEASE)
    result = project(metadata, [hit()])
    assert result["semantic_coverage"]["supported_roles"] == []
    assert result["semantic_coverage"]["field_support"][0]["status"] == "uncovered"


def test_boundary_real_shape_preserves_each_missing_manager_role():
    evidence = [hit(), hit(product=B, citation="citation.b")]
    first = project(contract(objects=(A, B), fields=(), roles=("manager",)), evidence)
    second = boundary_projection({**first, "public_release_id": RELEASE, "hits": evidence})
    assert second == first
    assert len(second["coverage_gaps"]) == 2


def test_boundary_later_hit_loss_cannot_resurrect_support():
    first = project(contract(), [hit()])
    second = boundary_projection({**first, "release_id": RELEASE, "hits": []}, trusted_public_product_ids={A})
    assert second["semantic_coverage"]["supported_roles"] == []
    assert second["semantic_coverage"]["field_support"][0]["status"] == "uncovered"
    assert "semantic_support_unavailable" in codes(second)


def test_boundary_untrusted_hit_loss_uses_generic_gap_without_object_id():
    first = project(contract(), [hit()])
    second = boundary_projection({**first, "release_id": RELEASE, "hits": []})
    assert second["semantic_coverage"]["objects"] == []
    assert codes(second) == {"provider_reported_gap"}
    assert A not in json.dumps(second)


def test_pure_functions_do_not_mutate_provider_or_verified_hits():
    raw = {"retrieval": {"semantic_coverage": contract()}, "coverage_gaps": []}
    hits = [hit()]
    before = deepcopy((raw, hits))
    result = consumer_projection(raw, hits, RELEASE)
    assert (raw, hits) == before
    result["semantic_coverage"]["field_support"][0]["citation_ids"].append("citation.changed")
    assert (raw, hits) == before


def test_dedupe_preserves_object_role_field_and_removes_only_exact_duplicates():
    gaps = [{"code": "unsupported_semantic_role", "product_id": A, "role": "manager"},
            {"code": "unsupported_semantic_role", "product_id": B, "role": "manager"},
            {"code": "unsupported_semantic_role", "product_id": A, "role": "custodian"},
            {"code": "semantic_field_uncovered", "product_id": A, "field": "type"},
            {"code": "semantic_field_uncovered", "product_id": A, "field": "placement"}]
    assert unique_semantic_gaps(gaps + deepcopy(gaps)) == gaps


def load_tests(loader, tests, pattern):
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        parametrized = getattr(function, "test_cases", None)
        if parametrized is None:
            tests.addTest(unittest.FunctionTestCase(function, description=name))
        else:
            argument, values = parametrized
            for index, value in enumerate(values):
                tests.addTest(unittest.FunctionTestCase(
                    update_wrapper(partial(function, **{argument: value}), function),
                    description=name + ":" + str(index)))
    return tests
