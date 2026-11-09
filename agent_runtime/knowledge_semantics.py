"""本地公共 RAG 语义覆盖的受限消费与出站合同。

``verified_hits`` 必须由消费方完成权威回查和原文、身份、许可、时点验证。
本模块重新约束引用集合与字段含义，不能把提供方自行标为 public 的载荷
变成权威证据。boundary_projection 的输入必须是消费方成功投影后的结果；
可信公共身份参数由本地调用者提供，不能从提供方或模型参数中取得。

覆盖元数据说明证据边界，不等于生成答案的逐事实校验。这里不调用模型，
不读取个人库，不输出查询、提供方消息、原字段值或排序诊断。
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import re
from types import MappingProxyType
from typing import Any

from local_knowledge.field_semantics import (
    FIELD_REGISTRY, ROLE_FIELDS, ROLE_PHRASES, SEMANTIC_POLICY,
)
from local_knowledge.vector_documents import _REFERENCES
from webank_catalog import URL as WEBANK_DISCLOSURE_URL


PROJECTION_POLICY = "knowledge-semantic-projection/1"
SEMANTIC_FIELDS = frozenset(FIELD_REGISTRY)
SEMANTIC_ROLES = frozenset(ROLE_PHRASES)
MAX_OBJECTS = 8
MAX_CITATIONS = 8
MAX_ENTRIES = 128
MAX_GAPS = 32
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

SEMANTIC_GAP_MESSAGES = MappingProxyType({
    "unsupported_semantic_role": "请求的机构角色尚无已登记原字段证据，不能用其他机构角色代替。",
    "semantic_field_uncovered": "请求的字段缺少本次已核验原文证据。",
    "semantic_field_reference_only": "该字段仅有资料指引，不能据此补出未披露的具体值。",
    "semantic_role_uncovered": "请求的机构角色缺少本次已核验原字段证据。",
    "context_incomplete": "本次证据的上下文覆盖不完整，不能据此补出缺失结论。",
    "semantic_support_unavailable": "原先声明的语义支持未通过本次引用复验，已降为证据缺口。",
    "semantic_contract_invalid": "语义覆盖元数据不符合受限消费合同，未沿用其中的支持声明。",
})
SEMANTIC_GAP_CODES = frozenset(SEMANTIC_GAP_MESSAGES)
_GENERIC_MESSAGE = "提供方报告了尚未解决的资料覆盖缺口。"


class _InvalidContract(ValueError):
    pass


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value))


def _enum_list(value: Any, allowed: frozenset[str], maximum: int) -> list[str]:
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(item, str) or item not in allowed for item in value)):
        raise _InvalidContract()
    return list(dict.fromkeys(value))


def _list(value: Any, maximum: int) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise _InvalidContract()
    return value


def _citations(value: Any) -> list[str]:
    values = _list(value, MAX_CITATIONS)
    if any(not _identifier(item) for item in values) or len(set(values)) != len(values):
        raise _InvalidContract()
    return values


def _gap(code: str, *, product_id=None, role=None, field=None, citation_id=None):
    result = {"code": code, "message": (
        _GENERIC_MESSAGE if code == "provider_reported_gap" else SEMANTIC_GAP_MESSAGES[code]
    )}
    for key, value in (("product_id", product_id), ("role", role),
                       ("field", field), ("citation_id", citation_id)):
        if value is not None:
            result[key] = value
    return result


def unique_semantic_gaps(items):
    """按对象、角色和字段分别去重；不因另一个对象有证据而抹掉缺口。"""
    seen, output = set(), []
    for item in items:
        key = tuple(item.get(key) for key in (
            "code", "product_id", "role", "field", "citation_id", "facet",
        ))
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output


def _verified_index(hits, release_id):
    """调用者已回查的成功集合；重复引用一律不能作为正支持。"""
    index, duplicates, seen = {}, set(), set()
    if not _identifier(release_id) or not isinstance(hits, (list, tuple)):
        return index
    if len(hits) > MAX_CITATIONS:
        return index
    for hit in hits:
        if not isinstance(hit, Mapping):
            continue
        citation = hit.get("citation_id")
        if not _identifier(citation):
            continue
        if citation in seen:
            duplicates.add(citation)
        seen.add(citation)
        if (hit.get("release_id") != release_id or hit.get("visibility") != "public"
                or hit.get("outbound_allowed") is not True
                or hit.get("withdrawn", False) is not False
                or hit.get("content_status") != "full_text"
                or not all(_identifier(hit.get(key)) for key in (
                    "document_id", "document_version_id", "parse_id", "span_id"))
                or (hit.get("product_id") is not None
                    and not _identifier(hit.get("product_id")))):
            continue
        index[citation] = hit
    for citation in duplicates:
        index.pop(citation, None)
    return index


def _field_proof(hit):
    """复验成功原文投影中的登记来源、精确单字段 JSON 和定位。

    成功消费投影不含 parser_id/shape；不能伪造这些字段后声称再次完成来源
    准入。该函数依赖上述权威回查前提，并用登记端点和原定位限制字段语义。
    """
    product = hit.get("product_id")
    locator, excerpt = hit.get("locator"), hit.get("excerpt")
    if (not isinstance(product, str) or not product.startswith("WEBANK:")
            or hit.get("channel_id") != "WEBANK" or hit.get("share_class_id") is not None
            or hit.get("source_url") != WEBANK_DISCLOSURE_URL
            or not isinstance(locator, Mapping) or locator.get("page") is not None
            or not isinstance(excerpt, str) or not 0 < len(excerpt) <= 1200):
        return None
    start, end = locator.get("start_char"), locator.get("end_char")
    if (type(start) is not int or type(end) is not int
            or not 0 <= start < end <= 10**9 or end - start != len(excerpt)):
        return None
    try:
        raw = json.loads("{" + excerpt + "}")
        if not isinstance(raw, dict) or len(raw) != 1:
            return None
        field, value = next(iter(raw.items()))
        if (field not in SEMANTIC_FIELDS or not isinstance(value, str) or not value.strip()
                or any(ord(char) < 32 for char in value)
                or locator.get("paragraph") != "productPresentation:" + product[7:] + "/" + field
                or json.dumps(field, ensure_ascii=False, separators=(",", ":")) + ":"
                + json.dumps(value, ensure_ascii=False, separators=(",", ":")) != excerpt):
            return None
        return field, "reference_only" if value.strip() in _REFERENCES else "disclosed_value"
    except (ValueError, TypeError, RecursionError):
        return None


def _support(citations, references, product, field, index):
    """整个声明必须是同一产品、发布与原件版本的成功引用子集。"""
    if not citations and not references:
        return None
    versions, documents = set(), set()
    for ids, status in ((citations, "disclosed_value"), (references, "reference_only")):
        for citation in ids:
            hit = index.get(citation)
            if (hit is None or hit.get("product_id") != product
                    or _field_proof(hit) != (field, status)):
                return None
            versions.add(hit["document_version_id"])
            documents.add(hit["document_id"])
    if len(versions) != 1 or len(documents) != 1 or set(citations) & set(references):
        return None
    return next(iter(versions))


def _safe_gaps(values, allowed_objects, index):
    result = []
    for item in _list(values, MAX_ENTRIES):
        if not isinstance(item, Mapping) or not isinstance(item.get("code"), str):
            raise _InvalidContract()
        code = item["code"]
        if code not in SEMANTIC_GAP_CODES:
            continue  # 普通提供方缺口由现有消费方处理，不能扩大全局出站字段。
        if code == "context_incomplete":
            result.append(_gap(code))
            continue
        product = item.get("product_id")
        if product is not None and product not in allowed_objects:
            result.append(_gap("provider_reported_gap"))
            continue
        role = item.get("role") if item.get("role") in SEMANTIC_ROLES else None
        field = item.get("field") if item.get("field") in SEMANTIC_FIELDS else None
        citation = item.get("citation_id") if item.get("citation_id") in index else None
        if code == "unsupported_semantic_role" and role in ROLE_FIELDS:
            code = "semantic_role_uncovered"
        result.append(_gap(code, product_id=product, role=role, field=field, citation_id=citation))
    return result


def _bound_version(record, version, release_id, *, required):
    if version is None:
        return None
    if required or "document_version_id" in record or "release_id" in record:
        if (record.get("document_version_id") != version
                or record.get("release_id") != release_id):
            return None
    return version


def _project(metadata, raw_gaps, hits, release_id, trusted_objects, *, require_bindings=False):
    index = _verified_index(hits, release_id)
    allowed_objects = {hit["product_id"] for hit in index.values()
                       if hit.get("product_id") is not None} | trusted_objects
    try:
        gaps = _safe_gaps(raw_gaps, allowed_objects, index)
        if metadata is None:
            if len(unique_semantic_gaps(gaps)) > MAX_GAPS:
                raise _InvalidContract()
            return {"coverage_gaps": unique_semantic_gaps(gaps), "semantic_coverage": None}
        if (not isinstance(metadata, Mapping) or type(metadata.get("schema_version")) is not int
                or metadata["schema_version"] != 1 or metadata.get("policy") != SEMANTIC_POLICY):
            raise _InvalidContract()
        fields = _enum_list(metadata.get("requested_fields"), SEMANTIC_FIELDS, len(SEMANTIC_FIELDS))
        roles = _enum_list(metadata.get("requested_roles"), SEMANTIC_ROLES, len(SEMANTIC_ROLES))
        _enum_list(metadata.get("unsupported_roles"), SEMANTIC_ROLES, len(SEMANTIC_ROLES))
        raw_objects = _list(metadata.get("objects"), MAX_OBJECTS)
        if any(not _identifier(item) for item in raw_objects):
            raise _InvalidContract()
        objects = sorted(set(raw_objects) & allowed_objects)
        if set(raw_objects) - allowed_objects:
            gaps.append(_gap("provider_reported_gap"))
        gaps.extend(_safe_gaps(metadata.get("coverage_gaps"), allowed_objects, index))
        field_records, role_records = {}, {}
        for record in _list(metadata.get("field_support"), MAX_ENTRIES):
            if (not isinstance(record, Mapping) or not _identifier(record.get("product_id"))
                    or record.get("field") not in SEMANTIC_FIELDS
                    or record.get("status") not in ("supported", "reference_only", "uncovered")):
                raise _InvalidContract()
            _citations(record.get("citation_ids"))
            _citations(record.get("reference_citation_ids"))
            key = record["product_id"], record["field"]
            field_records.setdefault(key, []).append(record)
        for record in _list(metadata.get("supported_roles"), MAX_ENTRIES):
            if (not isinstance(record, Mapping) or not _identifier(record.get("product_id"))
                    or record.get("role") not in SEMANTIC_ROLES
                    or record.get("original_field") not in SEMANTIC_FIELDS):
                raise _InvalidContract()
            _citations(record.get("citation_ids"))
            key = record["product_id"], record["role"]
            role_records.setdefault(key, []).append(record)
        field_support, supported_roles = [], []
        for product in objects:
            for field in fields:
                records = field_records.get((product, field), [])
                record = records[0] if len(records) == 1 else None
                status, citations, references, version = "uncovered", [], [], None
                claimed = any(item["status"] != "uncovered" or item["citation_ids"]
                              or item["reference_citation_ids"] for item in records)
                if record is not None:
                    proposed = record["status"]
                    positive, referential = record["citation_ids"], record["reference_citation_ids"]
                    if ((proposed == "supported" and positive)
                            or (proposed == "reference_only" and referential and not positive)):
                        version = _support(positive, referential, product, field, index)
                        version = _bound_version(record, version, release_id, required=require_bindings)
                        if version is not None:
                            status, citations, references = proposed, sorted(positive), sorted(referential)
                item = {"product_id": product, "field": field, "status": status,
                        "citation_ids": citations, "reference_citation_ids": references}
                if version is not None:
                    item.update(document_version_id=version, release_id=release_id)
                field_support.append(item)
                if status != "supported":
                    gaps.append(_gap("semantic_field_reference_only" if status == "reference_only"
                                     else "semantic_field_uncovered", product_id=product, field=field))
                if claimed and version is None:
                    gaps.append(_gap("semantic_support_unavailable", product_id=product, field=field))
            for role in roles:
                field = ROLE_FIELDS.get(role)
                if field is None:
                    gaps.append(_gap("unsupported_semantic_role", product_id=product, role=role))
                    continue
                records = role_records.get((product, role), [])
                record = records[0] if len(records) == 1 else None
                version = None
                if record is not None and record["original_field"] == field and record["citation_ids"]:
                    version = _support(record["citation_ids"], [], product, field, index)
                    version = _bound_version(record, version, release_id, required=require_bindings)
                if version is None:
                    gaps.append(_gap("semantic_role_uncovered", product_id=product, role=role, field=field))
                    if records:
                        gaps.append(_gap("semantic_support_unavailable", product_id=product, role=role, field=field))
                else:
                    supported_roles.append({"product_id": product, "role": role,
                                            "original_field": field,
                                            "citation_ids": sorted(record["citation_ids"]),
                                            "document_version_id": version, "release_id": release_id,
                                            "support_scope": "source_disclosed_role_only"})
        gaps = unique_semantic_gaps(gaps)
        if len(gaps) > MAX_GAPS:
            raise _InvalidContract()
        coverage = {"schema_version": 1, "policy": SEMANTIC_POLICY,
                    "projection_policy": PROJECTION_POLICY,
                    "objects": objects, "requested_fields": fields, "requested_roles": roles,
                    "unsupported_roles": [role for role in roles if role not in ROLE_FIELDS],
                    "supported_roles": supported_roles, "field_support": field_support,
                    "coverage_gaps": gaps,
                    "scope": "public_evidence_coverage_not_final_answer_validation"}
        return {"coverage_gaps": gaps, "semantic_coverage": coverage}
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        return {"coverage_gaps": [_gap("semantic_contract_invalid")], "semantic_coverage": None}


def _trusted_set(value):
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {item for item in value if _identifier(item)}


def consumer_projection(raw, verified_hits, release_id, *, request=None,
                        trusted_public_product_ids=()):
    """在引用回查完成后投影；可信无引用对象必须同时是本次公共请求主体。

    普通 gap 仍由消费方原合同处理。本函数只消费新语义 gap 与覆盖合同；
    不把检索诊断整包送给模型。ID 字符串格式、提供方 objects 均非公共身份依据。
    """
    if not isinstance(raw, Mapping):
        return {"coverage_gaps": [_gap("semantic_contract_invalid")], "semantic_coverage": None}
    trusted = _trusted_set(trusted_public_product_ids)
    product = request.get("product_id") if isinstance(request, Mapping) else None
    active_trusted = {product} if _identifier(product) and product in trusted else set()
    retrieval = raw.get("retrieval")
    metadata = retrieval.get("semantic_coverage") if isinstance(retrieval, Mapping) else None
    return _project(metadata, raw.get("coverage_gaps", []), verified_hits, release_id, active_trusted)


def boundary_projection(data, *, trusted_public_product_ids=()):
    """知识工具专用的第二次投影；不得用于任意提供方载荷或个人工具结果。

    data 必须来自成功消费投影，trusted_public_product_ids 必须由调用方的
    本次可信公共请求身份上下文提供。尚未通过消费方回查的对象不能靠字段
    白名单或 ID 形状获得出站资格。
    """
    if not isinstance(data, Mapping):
        return {"coverage_gaps": [_gap("semantic_contract_invalid")], "semantic_coverage": None}
    trusted = _trusted_set(trusted_public_product_ids)
    if ("public_release_id" in data and "release_id" in data
            and data["public_release_id"] != data["release_id"]):
        return {"coverage_gaps": [_gap("semantic_contract_invalid")], "semantic_coverage": None}
    release_id = data.get("public_release_id", data.get("release_id"))
    return _project(data.get("semantic_coverage"), data.get("coverage_gaps", []),
                    data.get("hits", []), release_id, trusted, require_bindings=True)


# A local answer condition derived from already verified public excerpts. It is
# separate from the catalog-only semantic_coverage protocol and grants no fact,
# personal entitlement, date calculation or operation authority.
_TIME_NUMBER = re.compile(r't\s*\+\s*([0-9]{1,4})(?![0-9])', re.I)
_TIME_DEFINITION = re.compile(
    r't\s*\+\s*n\s*(?:日|天)\s*[:：]?\s*(?:指|是|为)\s*'
    r'(?:自|从)?\s*t\s*日\s*(?:起|后)\s*(?:的)?\s*第\s*n\s*个\s*'
    r'(?P<unit>工作日|自然日)(?P<tail>[^。；;\n]{0,60})', re.I)
_TIME_UNASSERTED = re.compile(r'是否|能否|尚未|未核实|待核实|无法确认|不能确认|不确定|并非|不是|[?？]')
_TIME_EXCLUDES_ORIGIN = re.compile(r'不(?:包含|包括|含)\s*t\s*日', re.I)


def _time_normalized(value):
    import unicodedata
    return unicodedata.normalize('NFKC', value).lower()


def _time_definitions(text):
    result = []
    for match in _TIME_DEFINITION.finditer(_time_normalized(text)):
        result.append({'unit': match.group('unit'),
                       'excludes_t': True if _TIME_EXCLUDES_ORIGIN.search(match.group('tail')) else None,
                       'definition': match.group(0)})
    return result


def _concrete_time_conclusions(text):
    result = set()
    for clause in re.split(r'[，,。；;！!\n]', _time_normalized(text)):
        if _TIME_UNASSERTED.search(clause):
            continue  # Ambiguous/question/unknown mentions remain for GLM.
        for match in _TIME_NUMBER.finditer(clause):
            tail = clause[match.end():]
            head = re.sub(r'\s+', '', clause[:match.start()])
            after = re.match(
                r'\s*(?:(?:个)?(?:工作日|自然日)|日|天)?\s*'
                r'(?:\([^)]{0,30}\))?\s*(?:内|以内|之前|前|后)?\s*'
                r'(?:支付|划付|到账|结算)', tail)
            before = re.search(r'(?:支付|划付|到账|结算)(?:期限|时限|时间)?(?:为|是|在)?$', head)
            if after or before:
                result.add(str(int(match.group(1))))
    return result


# Concrete definitions are answer-only: source definitions remain generic so
# public_time_basis_bindings cannot borrow a different numerical horizon.
_TIME_NUMBERED_DEFINITION = re.compile(
    r't\s*\+\s*(?P<target>[0-9]{1,4})(?![0-9])\s*(?:日|天)\s*[:：]?\s*(?:指|是|为)\s*'
    r'(?:自|从)?\s*t\s*日\s*(?:起|后)\s*(?:的)?\s*第\s*(?P<ordinal>[0-9]{1,4})(?![0-9])\s*个\s*'
    r'(?P<unit>工作日|自然日)?'
    r'(?P<tail>(?:(?!t\s*\+\s*(?:[0-9]|n))[^。；;！!\n]){0,60})', re.I)


def _time_origin_states(text):
    # Keep unknown origin wording unknown, and remove entire negative spans
    # before checking positive wording: 不包含T日 must not also mean 含T日.
    compact = re.sub(r'\s+', '', _time_normalized(text))
    compact = re.split(r't\+(?:[0-9]|n)', compact, maxsplit=1)[0]
    excluded = re.compile(r'不(?:包含|包括|含|计入|算入)t日|t日不(?:计入|算入)')
    states = set()
    for clause in re.split(r'[，,。；;！!\n]', compact):
        values = {True} if excluded.search(clause) else set()
        remaining = excluded.sub('', clause)
        if re.search(r'(?:包含|包括|含|计入|算入)t日|t日(?:也|同样)?(?:计入|算入)', remaining):
            values.add(False)
        origin_unknown = re.search(r'起点|起(?:算|始)(?:日|点)?|计日(?:口径|单位)|单位', clause)
        if (values or origin_unknown) and (
                _TIME_UNASSERTED.search(clause) or re.search(r'未知|不明|未明确', clause)):
            states.add(None)
        else:
            states.update(values)
    return states


def _answer_time_basis_status(text, number, unit, excludes_t):
    normalized = _time_normalized(text)
    compact = re.sub(r'\s+', '', normalized)
    units, origin = set(), False
    numbered = [match for match in _TIME_NUMBERED_DEFINITION.finditer(normalized)
                if int(match.group('target')) == int(number)]
    numbered_origins = set()
    for match in numbered:
        remainder = normalized[match.end():]
        if remainder and not re.match(r'[。；;！!\n]|t\s*\+\s*(?:[0-9]|n)', remainder):
            return 'missing'  # The bounded tail ended inside this definition.
        prefix = re.split(r'[，,。；;！!\n]', normalized[:match.start()])[-1]
        assertion = (prefix + normalized[match.start():match.start('tail')]
                     + re.split(r'[，,]', match.group('tail'))[0])
        unit_tail = match.group('tail')
        if unit == '工作日':
            # Only a complete local comparison clause negates the other unit.
            # Keep later affirmative units and all original origin checks.
            unit_tail = re.sub(
                r'(^|[，,])\s*(?:并非|不是)\s*自然日(?=\s*(?:[，,]|$))',
                r'\1', unit_tail)
        if (int(match.group('ordinal')) != int(number)
                or match.group('unit') != unit
                or _TIME_UNASSERTED.search(assertion)
                or re.search(r'未知|不明|未明确', assertion)
                or set(re.findall(r'工作日|自然日', unit_tail)) - {unit}):
            return 'missing'
        units.add(match.group('unit'))
        numbered_origins.update(_time_origin_states(match.group('tail')))
    for definition in _time_definitions(text):
        units.add(definition['unit'])
        origin = origin or definition['excludes_t'] is True
        if numbered:
            generic = _TIME_DEFINITION.fullmatch(definition['definition'])
            numbered_origins.update(_time_origin_states(generic.group('tail')))
    literal = re.escape(number)
    direct = re.compile(r't\+0*' + literal + r'(?![0-9])'
        r'(?:(?:日|天)(?:内|以内)?)?(?:\()?(?:按|以)?'
        r'(?P<unit>(?:个)?工作日|(?:个)?自然日)')
    for match in direct.finditer(compact):
        units.add(match.group('unit').removeprefix('个'))
        nearby = re.split(r'[。；;！!\n]', compact[match.start():match.end()+80])[0]
        origin = origin or _TIME_EXCLUDES_ORIGIN.search(nearby) is not None
        if numbered:
            local = nearby[match.end()-match.start():]
            numbered_origins.update(_time_origin_states(local))
    if numbered:
        # Unknown or contradictory origin cannot borrow a correct unit/phrase.
        # The usual generic/direct path is byte-for-byte unchanged otherwise.
        return ('explicit' if units == {unit} and type(excludes_t) is bool
                and numbered_origins == {excludes_t} else 'missing')
    if units == {unit}:
        return ('explicit' if excludes_t is None or origin else 'semantic_review_required')
    if units:
        return 'missing'  # A different/contradictory counting unit is not support.
    # Do not declare an anaphoric paraphrase complete based on word presence.
    # A potentially equivalent linkage is left to the original GLM reviewer.
    if re.search(r'(?:上述|以上|该|这一)(?:期限|时限|时间|计日口径).{0,12}'
                 r'(?:工作日|自然日).{0,6}(?:计|计算|口径)', compact):
        return 'semantic_review_required'
    return 'missing'


def public_time_basis_bindings(final, evidence):
    """Bind affirmative concrete time conclusions to same-version definitions.

    Callers supply only request-local consumer-verified public evidence after
    their normal fresh/privacy checks. This does not validate a provider by its
    public label. Claims-only quotes and a postponement answer without a
    concrete limit create no obligation. No deadline, unit or text is invented.
    """
    if not isinstance(final, Mapping) or not isinstance(final.get('text'), str):
        return []
    conclusions = _concrete_time_conclusions(final['text'])
    if not conclusions or not isinstance(evidence, (list, tuple)):
        return []
    cited = {eid for claim in final.get('claims', []) if isinstance(claim, Mapping)
             for eid in claim.get('evidence_ids', []) if isinstance(eid, str)}
    groups = {}
    for row in evidence[:48]:
        if (not isinstance(row, Mapping) or row.get('tool') != 'knowledge_search'
                or row.get('source') != 'public_knowledge' or row.get('id') not in cited):
            continue
        data = row.get('data')
        if (not isinstance(data, Mapping) or data.get('scope') != 'public'
                or data.get('ready') is not True or data.get('status') not in ('ready', 'partial')):
            continue
        release = data.get('public_release_id')
        for hit in _verified_index(data.get('hits'), release).values():
            if (not _identifier(hit.get('product_id'))
                    or not isinstance(hit.get('excerpt'), str)
                    or not isinstance(hit.get('facets'), list)
                    or not {'liquidity', 'terms'} & set(hit['facets'])):
                continue
            key = tuple(hit.get(k) for k in ('product_id', 'share_class_id', 'channel_id',
                'document_id', 'document_version_id', 'release_id'))
            group = groups.setdefault(key, {'limits': {}, 'definitions': []})
            compact = re.sub(r'\s+', '', _time_normalized(hit['excerpt']))
            for match in _TIME_NUMBER.finditer(compact):
                nearby = compact[max(0, match.start()-32):match.end()+48]
                if re.search(r'支付|划付|到账|结算', nearby):
                    group['limits'].setdefault(str(int(match.group(1))), []).append((row['id'], hit))
            group['definitions'].extend((row['id'], hit, definition)
                for definition in _time_definitions(hit['excerpt']))
    output = []
    for number in sorted(conclusions, key=int):
        applicable = [(key, group) for key, group in groups.items() if number in group['limits']]
        if len(applicable) != 1:
            continue  # Multiple subjects/versions cannot borrow each other's basis.
        key, group = applicable[0]
        definitions = group['definitions']
        meanings = {(d['unit'], d['excludes_t']) for _, _, d in definitions}
        if len(meanings) != 1:
            continue
        unit, excludes = next(iter(meanings))
        limit_eid, limit_hit = group['limits'][number][0]
        definition_eid, definition_hit, definition = definitions[0]
        output.append({'notation': 'T+' + number, 'counting_unit': unit,
            'excludes_t': excludes,
            **dict(zip(('product_id', 'share_class_id', 'channel_id', 'document_id',
                       'document_version_id', 'release_id'), key)),
            'limit_evidence_id': limit_eid, 'limit_citation_id': limit_hit['citation_id'],
            'definition_evidence_id': definition_eid,
            'definition_citation_id': definition_hit['citation_id'],
            'definition': definition['definition'],
            'answer_status': _answer_time_basis_status(final['text'], number, unit, excludes),
            'location': 'text', 'scope': 'source_time_condition_not_execution_or_current_validity'})
    return output
