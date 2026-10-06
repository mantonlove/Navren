"""公共 RAG 的只读消费合同；实现合同不表示真实 RAG 已建成。

提供方只能由本地受信应用注入，不能由模型选择模块、路径或连接参数。
本模块不检索账户或私有文档，不读取向量载荷正文，也不开放任意文件读取。

协议版本 1：
* status() 返回 schema_version、ready、provider_id、public_release_id。
* search_public(request, release_id) 仅返回 schema_version、release_id、
  citation_ids 和 coverage_gaps；request 的 scope 固定为 public。
* resolve_public_evidence(citation_id, release_id, as_of) 在 SQLite 等权威
  记录中回查发布成员、产品身份、原件/解析版本、来源当前状态与出站策略。
  撤回/禁止/不存在的证据返回 None；不可直接返回向量缓存的载荷。

canonical 证据字段见 _project_evidence；所有字段必须来自同一次权威回查。
locator 的字符区间为解析原文的半开区间，必须对应该短 excerpt 的完整范围；
提供方负责逐字核对。日期为 YYYY-MM-DD；效期为 [from, to)。空结束日期
只表示终止时间未知。检索条款不证明当前渠道销售状态或交易授权。

本模块验证合同与安全投影，无法替代提供方的身份映射、许可和原文核验。
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime
from ipaddress import ip_address
import re
from typing import Any, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
MAX_HITS = 8
MAX_QUERY_CHARS = 500
MAX_EXCERPT_CHARS = 1200
MAX_GAPS = 32
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
FACETS = frozenset({"fees", "liquidity", "risk", "strategy", "eligibility",
                    "sale_status", "returns", "general", "terms"})


class PublicKnowledgeProvider(Protocol):
    """本地适配方的最小接口；不把检索分数解释为事实正确概率。"""

    def status(self) -> Mapping[str, Any]: ...

    def search_public(self, request: Mapping[str, Any],
                      release_id: str) -> Mapping[str, Any]: ...

    def resolve_public_evidence(self, citation_id: str, release_id: str,
                                as_of: str) -> Mapping[str, Any] | None: ...


GAP_MESSAGES = {
    "provider_missing": "公共 RAG 提供方尚未接入。",
    "provider_not_ready": "公共 RAG 尚无可用的已核验发布。",
    "provider_error": "公共 RAG 调用失败，未返回未经核验的内容。",
    "invalid_provider_status": "公共 RAG 状态不符合消费合同。",
    "invalid_request": "检索参数不符合公共证据消费合同。",
    "invalid_search_response": "检索返回的版本或字段不符合消费合同。",
    "provider_limit_exceeded": "提供方返回超过请求上限的候选，超出部分未读取。",
    "invalid_citation": "候选引用标识不合法，未读取。",
    "duplicate_hit": "候选引用重复，已去重。",
    "evidence_unavailable": "原文引用不存在、已撤回或不再允许读取。",
    "resolver_error": "权威原文回查失败，该引用未输出。",
    "invalid_evidence": "原文引用的字段或定位不符合消费合同。",
    "release_mismatch": "候选引用与固定发布版本不一致。",
    "citation_mismatch": "原文回查与请求引用不一致。",
    "private_evidence": "私有证据不能进入公共云端对话检索。",
    "outbound_forbidden": "该原文片段未获准出站。",
    "withdrawn_evidence": "该原文来源已撤回。",
    "metadata_only": "该来源只有元数据，不能作为正文条款证据。",
    "subject_mismatch": "原文主体与请求产品、份额或渠道不一致。",
    "not_yet_effective": "原文条款在查询时点尚未生效。",
    "expired_evidence": "原文条款在查询时点已经到期。",
    "published_after_as_of": "原文公布晚于查询时点，不能用于该时点判断。",
    "effective_period_unknown": "原文有效区间不完整，不能推断当前仍有效。",
    "publication_date_unknown": "原文发布日期未知，不能确认其在查询时点已公开。",
    "facet_uncovered": "请求的分析维度缺少已核验原文证据。",
    "source_unavailable": "所需公开来源暂无可用证据。",
    "source_permission_missing": "所需来源的读取或出站许可未核实。",
    "identity_unverified": "所需产品、份额或渠道身份尚未核实。",
    "conflicting_sources": "公开来源存在冲突，需要保留差异并进一步核实。",
    "sale_status_unknown": "条款检索不能证明当前渠道在售或可交易。",
    "not_enough_evidence": "当前原文证据不足以完整回答问题。",
    "provider_reported_gap": "提供方报告了尚未解决的资料覆盖缺口。",
    "no_verified_evidence": "本次没有可输出的已核验公共原文证据。",
}


class _ContractError(ValueError):
    def __init__(self, code: str = "invalid_evidence"):
        self.code = code
        super().__init__(code)


def _today() -> date:
    # 公共产品证据使用现实市场时间，不能使用模拟银行时钟。
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise _ContractError()
    return value


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _ContractError()
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise _ContractError()
    return value


def _day(value: Any, *, nullable: bool = False) -> date | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise _ContractError()
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise _ContractError() from error


def _source_url(value: Any) -> str:
    value = _text(value, 2048)
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if parts.scheme not in ("https", "http") or not host or parts.username or parts.password:
            raise _ContractError()
        if any(c.isspace() for c in value) or "\\" in value:
            raise _ContractError()
        _ = parts.port  # 无效端口也不能进入引用。
        host = host.rstrip(".").lower()
        if "." not in host or host.endswith((".local", ".localhost", ".internal")):
            raise _ContractError()
        try:
            address = ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise _ContractError()
    except (ValueError, TypeError) as error:
        raise _ContractError() from error
    return value


def _gap(code: str, *, citation_id: str | None = None,
         facet: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": GAP_MESSAGES[code]}
    if citation_id is not None:
        result["citation_id"] = citation_id
    if facet is not None:
        result["facet"] = facet
    return result


def _provider_gaps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_GAPS:
        raise _ContractError("invalid_search_response")
    result = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("code"), str):
            raise _ContractError("invalid_search_response")
        # 不输出提供方错误消息/任意上下文，避免路径、账户或私有输入通过缺口泄露。
        code = item["code"] if item["code"] in GAP_MESSAGES else "provider_reported_gap"
        facet = item.get("facet") if item.get("facet") in FACETS else None
        result.append(_gap(code, facet=facet))
    return result


def _unique_gaps(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for item in items:
        key = (item["code"], item.get("citation_id"), item.get("facet"))
        if key not in seen:
            seen.add(key)
            output.append(item)
        if len(output) == MAX_GAPS:
            break
    return output


def _project_evidence(value: Any, citation_id: str, release_id: str,
                      request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or type(value.get("schema_version")) is not int:
        raise _ContractError()
    if value["schema_version"] != SCHEMA_VERSION:
        raise _ContractError()
    if value.get("citation_id") != citation_id:
        raise _ContractError("citation_mismatch")
    if value.get("release_id") != release_id:
        raise _ContractError("release_mismatch")
    if value.get("visibility") != "public":
        raise _ContractError("private_evidence")
    if value.get("outbound_allowed") is not True:
        raise _ContractError("outbound_forbidden")
    if value.get("withdrawn") is not False:
        raise _ContractError("withdrawn_evidence")
    if value.get("content_status") != "full_text":
        raise _ContractError("metadata_only")

    hit: dict[str, Any] = {
        "citation_id": citation_id, "release_id": release_id,
        "document_id": _identifier(value.get("document_id")),
        "document_version_id": _identifier(value.get("document_version_id")),
        "parse_id": _identifier(value.get("parse_id")),
        "span_id": _identifier(value.get("span_id")),
        "title": _text(value.get("title"), 240),
        "publisher": _text(value.get("publisher"), 160),
        "source_url": _source_url(value.get("source_url")),
        "excerpt": _text(value.get("excerpt"), MAX_EXCERPT_CHARS),
        "visibility": "public", "outbound_allowed": True,
        "content_status": "full_text",
    }
    for field in ("product_id", "share_class_id", "channel_id"):
        if field not in value:
            raise _ContractError()
        hit[field] = None if value[field] is None else _identifier(value[field])
        if request.get(field) is not None and hit[field] != request[field]:
            raise _ContractError("subject_mismatch")

    locator = value.get("locator")
    if not isinstance(locator, Mapping):
        raise _ContractError()
    page, paragraph = locator.get("page"), locator.get("paragraph")
    if page is not None and (type(page) is not int or not 1 <= page <= 100000):
        raise _ContractError()
    if paragraph is not None:
        paragraph = _text(paragraph, 160)
    if page is None and paragraph is None:
        raise _ContractError()
    start, end = locator.get("start_char"), locator.get("end_char")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= 10**9:
        raise _ContractError()
    if end - start != len(hit["excerpt"]):
        raise _ContractError()
    hit["locator"] = {"page": page, "paragraph": paragraph,
                      "start_char": start, "end_char": end}

    days = {}
    for field in ("published_on", "retrieved_on", "effective_from", "effective_to"):
        if field not in value:
            raise _ContractError()
        days[field] = _day(value[field], nullable=field != "retrieved_on")
        hit[field] = value[field]
    current = _day(request["as_of"])
    begin, end_day = days["effective_from"], days["effective_to"]
    if begin is not None and end_day is not None and begin >= end_day:
        raise _ContractError()
    if days["published_on"] is not None and days["published_on"] > current:
        raise _ContractError("published_after_as_of")
    if begin is not None and current < begin:
        raise _ContractError("not_yet_effective")
    if end_day is not None and current >= end_day:
        raise _ContractError("expired_evidence")
    hit["effective_status"] = ("within_documented_interval"
                               if begin is not None and end_day is not None else "unknown")

    conditions = value.get("conditions")
    facets = value.get("facets")
    if not isinstance(conditions, list) or len(conditions) > 8:
        raise _ContractError()
    if not isinstance(facets, list) or len(facets) > MAX_HITS or any(f not in FACETS for f in facets):
        raise _ContractError()
    hit["conditions"] = [_text(c, 240) for c in conditions]
    hit["facets"] = list(dict.fromkeys(facets))
    hit["sale_status"] = "unknown"
    hit["claims_are_account_facts"] = False
    return hit


class KnowledgeRetrieval:
    def __init__(self, provider: PublicKnowledgeProvider | None = None):
        self.provider = provider
        self._pinned_status: dict[str, Any] | None = None
        self._delivered: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}

    def assert_fresh(self) -> None:
        """展示答案前重新验证本实例已提供证据；失败时拒绝沿用旧引用。

        对已投影片段再次检查当前撤回/许可状态，不用发布切换代替撤回检查。
        状态查询可以看到新的发布，但本次会话所有证据仍固定同一发布。
        """
        try:
            # A request provider may also pin catalogue identities before any
            # citation is delivered. This hook is injected by trusted local code.
            provider_fresh = getattr(self.provider, 'assert_fresh', None)
            if callable(provider_fresh):
                provider_fresh()
            if not self._delivered:
                return
            status = self._status()
            if not status["ready"] or status["provider_id"] != self._pinned_status["provider_id"]:
                raise _ContractError()
            for (citation_id, as_of), (snapshot, request) in self._delivered.items():
                canonical = self.provider.resolve_public_evidence(
                    citation_id, snapshot["release_id"], as_of)
                current = _project_evidence(canonical, citation_id, snapshot["release_id"], request)
                if current != snapshot:
                    raise _ContractError()
        except Exception:
            raise ValueError("stale_knowledge_evidence") from None

    def _status(self) -> dict[str, Any]:
        base = {"schema_version": SCHEMA_VERSION, "scope": "public",
                "ready": False, "provider_id": None, "public_release_id": None}
        if self.provider is None:
            return {**base, "status": "unavailable", "coverage_gaps": [_gap("provider_missing")]}
        try:
            raw = self.provider.status()
        except Exception:
            return {**base, "status": "unavailable", "coverage_gaps": [_gap("provider_error")]}
        try:
            if (not isinstance(raw, Mapping) or type(raw.get("schema_version")) is not int
                    or raw["schema_version"] != SCHEMA_VERSION or type(raw.get("ready")) is not bool):
                raise _ContractError()
            provider_id = _identifier(raw.get("provider_id"))
            if not raw["ready"]:
                return {**base, "provider_id": provider_id, "status": "unavailable",
                        "coverage_gaps": [_gap("provider_not_ready")]}
            release_id = _identifier(raw.get("public_release_id"))
            return {**base, "ready": True, "provider_id": provider_id,
                    "public_release_id": release_id, "status": "ready", "coverage_gaps": []}
        except (_ContractError, TypeError, ValueError):
            return {**base, "status": "unavailable", "coverage_gaps": [_gap("invalid_provider_status")]}

    def status(self) -> dict[str, Any]:
        result = self._status()
        result["private_retrieval_available"] = False
        result["notice"] = ("只读公共证据消费接口；不证明资料完整、当前在售或真实交易能力。"
                            "提供方未接入时不会生成命中。")
        return result

    def search(self, query: str, product_id: str | None = None,
               share_class_id: str | None = None, channel_id: str | None = None,
               as_of: str | None = None, facets: list[str] | None = None,
               limit: int = 5) -> dict[str, Any]:
        self.assert_fresh()
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "status": "unavailable", "ready": False,
            "scope": "public", "provider_id": None, "public_release_id": None,
            "as_of": None, "market_timezone": "Asia/Shanghai", "hits": [],
            "coverage_gaps": [], "rejected_count": 0,
            "notice": "检索条款不等于当前在售、适合购买或交易授权；公开引文不是账户事实。",
        }
        try:
            request = {"scope": "public", "query": _text(query, MAX_QUERY_CHARS),
                       "as_of": _today().isoformat() if as_of is None else _day(as_of).isoformat(),
                       "limit": limit}
            if type(limit) is not int or not 1 <= limit <= MAX_HITS:
                raise _ContractError()
            if facets is None:
                facets = []
            if not isinstance(facets, list) or len(facets) > MAX_HITS or any(f not in FACETS for f in facets):
                raise _ContractError()
            request["facets"] = list(dict.fromkeys(facets))
            for key, value in (("product_id", product_id), ("share_class_id", share_class_id),
                               ("channel_id", channel_id)):
                if value is not None:
                    request[key] = _identifier(value)
        except (_ContractError, TypeError, ValueError):
            result["coverage_gaps"] = [_gap("invalid_request")]
            return result
        result["as_of"] = request["as_of"]
        status = self._pinned_status or self._status()
        result.update({k: status[k] for k in ("ready", "provider_id", "public_release_id")})
        if not status["ready"]:
            result["coverage_gaps"] = status["coverage_gaps"]
            return result
        if self._pinned_status is None:
            self._pinned_status = deepcopy(status)
        release_id = status["public_release_id"]
        try:
            raw = self.provider.search_public(request, release_id)
        except Exception:
            result["ready"] = False
            result["coverage_gaps"] = [_gap("provider_error")]
            return result
        try:
            if (not isinstance(raw, Mapping) or type(raw.get("schema_version")) is not int
                    or raw["schema_version"] != SCHEMA_VERSION or raw.get("release_id") != release_id):
                raise _ContractError("invalid_search_response")
            ids = raw.get("citation_ids")
            if not isinstance(ids, list) or len(ids) > 64:
                raise _ContractError("invalid_search_response")
            gaps = _provider_gaps(raw.get("coverage_gaps"))
        except (_ContractError, TypeError, ValueError):
            result["ready"] = False
            result["coverage_gaps"] = [_gap("invalid_search_response")]
            return result
        if len(ids) > limit:
            result["rejected_count"] = len(ids) - limit
            gaps.append(_gap("provider_limit_exceeded"))
        seen = set()
        for candidate in ids[:limit]:
            citation_id = None
            try:
                citation_id = _identifier(candidate)
                if citation_id in seen:
                    raise _ContractError("duplicate_hit")
                seen.add(citation_id)
            except _ContractError as error:
                result["rejected_count"] += 1
                gaps.append(_gap("invalid_citation" if citation_id is None else error.code,
                                 citation_id=citation_id))
                continue
            try:
                canonical = self.provider.resolve_public_evidence(citation_id, release_id, request["as_of"])
            except Exception:
                result["rejected_count"] += 1
                gaps.append(_gap("resolver_error", citation_id=citation_id))
                continue
            try:
                if canonical is None:
                    raise _ContractError("evidence_unavailable")
                hit = _project_evidence(canonical, citation_id, release_id, request)
            except (_ContractError, TypeError, ValueError) as error:
                result["rejected_count"] += 1
                gaps.append(_gap(error.code if isinstance(error, _ContractError) else "invalid_evidence",
                                 citation_id=citation_id))
                continue
            key = (citation_id, request["as_of"])
            previous = self._delivered.get(key)
            # 同一引用不能用后查到的新版本覆盖本轮已交付的旧证据。
            # search_public/resolve 期间也可能修订，故不能只依赖入口的回查。
            if previous is not None and previous[0] != hit:
                raise ValueError("stale_knowledge_evidence")
            self._delivered.setdefault(key, (deepcopy(hit), deepcopy(request)))
            result["hits"].append(hit)
            if hit["effective_status"] == "unknown":
                gaps.append(_gap("effective_period_unknown", citation_id=citation_id))
            if hit["published_on"] is None:
                gaps.append(_gap("publication_date_unknown", citation_id=citation_id))
        covered = {facet for hit in result["hits"] for facet in hit["facets"]}
        gaps.extend(_gap("facet_uncovered", facet=facet) for facet in request["facets"] if facet not in covered)
        if not result["hits"]:
            gaps.append(_gap("no_verified_evidence"))
        result["coverage_gaps"] = _unique_gaps(gaps)
        result["status"] = ("partial" if result["hits"] and result["coverage_gaps"]
                            else "ready" if result["hits"] else "empty")
        return result
