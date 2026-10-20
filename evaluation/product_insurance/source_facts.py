from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from evaluation.product_insurance.harness import ROOT, AcceptanceError, read_json, sha256_file


RESEARCH = ROOT / "outputs/product-insurance-design-research-20261018"
BASELINE_PATH = ROOT / "evaluation/product_insurance/source-evidence-baseline.json"


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _html_text(path: Path, encoding: str) -> str:
    parser = _VisibleText()
    parser.feed(path.read_bytes().decode(encoding, errors="replace"))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _catalogue() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    raw = (RESEARCH / "raw/catalog.jsonp").read_text(encoding="utf-8")
    start, end = raw.find("("), raw.rfind(")")
    if start < 0 or end <= start:
        raise AcceptanceError("catalog_jsonp_wrapper_invalid")
    parsed = json.loads(raw[start + 1:end])
    rows = {row["code"]: row for row in parsed["productPresentation"]}
    return parsed, rows


def _verify_pdf_page1() -> str:
    pdf = RESEARCH / "raw/chinaamc-003003-summary.pdf"
    if shutil.which("pdftotext") is None:
        raise AcceptanceError("pdftotext_missing_pdf_field_locator_unverified")
    completed = subprocess.run(["pdftotext", "-f", "1", "-l", "1", "-layout", str(pdf), "-"], check=True, capture_output=True, text=True)
    text = re.sub(r"\s+", " ", completed.stdout)
    required = ["基金代码 003003", "基金管理人 华夏基金管理有限公司", "下属基金简称 华夏现金增利货币 A/E"]
    missing = [token for token in required if token not in text]
    if missing:
        raise AcceptanceError("003003_official_pdf_page1_locator_mismatch:" + ",".join(missing))
    return text


def _verify_zhongan_pdf_page1() -> str:
    pdf = RESEARCH / "raw/zhongan-medical-drugs-2026.pdf"
    if shutil.which("pdftotext") is None:
        raise AcceptanceError("pdftotext_missing_zhongan_pdf_field_locator_unverified")
    completed = subprocess.run(["pdftotext", "-f", "1", "-l", "1", "-layout", str(pdf), "-"], check=True, capture_output=True, text=True)
    text = re.sub(r"\s+", "", completed.stdout)
    required = ["更新日期：2026年6月", "启用/执行日期：2026年6月23日下午17:00:00"]
    missing = [token for token in required if token not in text]
    if missing:
        raise AcceptanceError("zhongan_supplement_page1_locator_mismatch:" + ",".join(missing))
    return text


def verify_source_facts() -> dict[str, Any]:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    errors: list[str] = []
    facts: dict[str, Any] = {}

    catalog, rows = _catalogue()
    facts["catalog_extract_date"] = catalog.get("extractDate")
    expected_rows = baseline["catalog_rows"]
    for code, expected in expected_rows.items():
        row = rows.get(code)
        if row is None:
            errors.append(f"catalog_row_missing:{code}")
            continue
        observed = {field: row.get(field) for field in expected}
        facts[code] = observed
        if observed != expected:
            errors.append(f"catalog_row_field_mismatch:{code}")
    if catalog.get("extractDate") != baseline["catalog_extract_date"]:
        errors.append("catalog_extract_date_mismatch")

    for code, name in (("002604", "chinaamc-002604.html"), ("008213", "chinaamc-008213.html")):
        page = RESEARCH / "raw" / name
        text = _html_text(page, "gbk")
        compact = re.sub(r"\s+", "", text)
        expected = baseline["manager_page_markers"][code]
        absent = [marker for marker in expected if marker not in compact]
        facts[f"manager_page_{code}"] = {"path": str(page.relative_to(ROOT)), "sha256": sha256_file(page), "verified_markers": [m for m in expected if m in compact]}
        if absent:
            errors.append(f"manager_page_marker_missing:{code}:{','.join(absent)}")

    pdf_text = _verify_pdf_page1()
    facts["003003_official_pdf"] = {"path": "outputs/product-insurance-design-research-20261018/raw/chinaamc-003003-summary.pdf", "sha256": sha256_file(RESEARCH / "raw/chinaamc-003003-summary.pdf"), "page": 1, "locator": "一、产品概况 rows 基金代码/基金管理人/下属基金简称", "checks_passed": True}

    jsfund_path = RESEARCH / "raw/jsfund-money.html"
    jsfund_text = _html_text(jsfund_path, "utf-8")
    jsfund_compact = re.sub(r"\s+", "", jsfund_text)
    jsfund_markers = baseline["jsfund_identity_markers"]
    absent = [x for x in jsfund_markers if x not in jsfund_compact]
    facts["000581_identity_corroboration"] = {"path": str(jsfund_path.relative_to(ROOT)), "sha256": sha256_file(jsfund_path), "verified_markers": [x for x in jsfund_markers if x in jsfund_compact], "scope_limit": "嘉实历史帮助页仅用于名称/代码身份佐证；不证明当前微众渠道额度、销售状态或赎回条件。"}
    if absent:
        errors.append("000581_official_help_page_locator_missing")

    audit = read_json(RESEARCH / "catalog-audit-report.json")
    fee = next((x for x in audit.get("checks", []) if x.get("category") == "value_completeness"), {})
    fees_ok = fee.get("fee_meaningful_count") == 0 and fee.get("fee_population") == 13680
    insurance = next((x for x in audit.get("checks", []) if x.get("category") == "numeric_identity"), {})
    insurance_counts = insurance.get("evidence", {})
    if not fees_ok:
        errors.append("fee_audit_expectation_mismatch")
    if (insurance_counts.get("insurance_total"), insurance_counts.get("personal"), insurance_counts.get("institutional")) != (32, 8, 24):
        errors.append("insurance_population_expectation_mismatch")
    facts["audit_facts"] = {"catalog_rows": 13680, "meaningful_numeric_fees": 0, "insurance_total": 32, "personal_insurance": 8, "institutional_insurance": 24, "audit_source_sha256": sha256_file(RESEARCH / "catalog-audit-report.json"), "locator": "checks.category=value_completeness/numeric_identity"}

    za = rows.get("ZA260501", {})
    if za.get("charge") != "以保险公司核定为准" or za.get("investor") != "以保险公司核保规则为准":
        errors.append("za260501_catalog_disclosure_marker_mismatch")
    za_pdf = RESEARCH / "raw/zhongan-medical-drugs-2026.pdf"
    if not baseline["zhongan_pdf_version_markers"]:
        errors.append("zhongan_pdf_locator_expectations_missing")
    _verify_zhongan_pdf_page1()
    facts["ZA260501_supplement"] = {"path": str(za_pdf.relative_to(ROOT)), "sha256": sha256_file(za_pdf), "page": 1, "locator": "更新日期/启用执行日期", "complete_contract_proven": False}

    expected_hashes = baseline.get("source_sha256", {})
    for relative, expected in expected_hashes.items():
        path = ROOT / relative
        if not path.is_file() or sha256_file(path) != expected:
            errors.append(f"source_evidence_hash_mismatch:{relative}")

    return {"schema": "navren.product-insurance.source-fact-check/1", "status": "PASS_SOURCE_SNAPSHOT_FACTS" if not errors else "FAIL_SOURCE_SNAPSHOT_FACTS", "source_date": "2026-10-14 catalogue; 2026-10-16 manager pages; retrieved 2026-10-18", "errors": errors, "facts": facts, "product_acceptance": False, "current_purchase_eligibility": False, "complete_insurance_contract": False}


def main() -> int:
    report = verify_source_facts()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS_SOURCE_SNAPSHOT_FACTS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
