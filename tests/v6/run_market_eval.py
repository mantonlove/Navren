#!/usr/bin/env python3
"""
v6 市场化生存评分编排。
读取 evidence/*.json → 检查证据完整性 → 逐项打分。
"""

import json, os, sys, time

# Ensure we're in the project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(PROJECT_ROOT)


def load_json(path):
    """加载 JSON 文件"""
    with open(path) as f:
        return json.load(f)


def check_evidence_file(evidence_dir, filename):
    """检查证据文件是否存在、是否包含有效数据"""
    path = os.path.join(evidence_dir, filename)
    if not os.path.exists(path):
        return {
            "exists": False,
            "evidence_level": "missing",
            "score_cap": 0,
            "detail": f"证据文件缺失: {filename}"
        }

    data = load_json(path)
    evidence_level = data.get('evidence_level', 'unknown')

    if evidence_level == 'placeholder':
        return {
            "exists": True,
            "evidence_level": "placeholder",
            "score_cap": 60,  # placeholder 最多60%
            "detail": f"占位证据(待验证): {data.get('title', filename)}",
            "missing_fields": data.get('required_fields', []),
            "status": data.get('status', '未知')
        }
    elif evidence_level == 'real':
        return {
            "exists": True,
            "evidence_level": "real",
            "score_cap": 100,
            "detail": f"真实证据: {data.get('title', filename)}",
            "missing_fields": [],
            "status": data.get('status', '已完成')
        }
    else:
        return {
            "exists": True,
            "evidence_level": "unknown",
            "score_cap": 30,
            "detail": f"证据等级未知: {filename}",
            "missing_fields": data.get('required_fields', []),
            "status": data.get('status', '未知')
        }


def check_required_fields(evidence_dir, filename, required_fields):
    """检查证据文件中的必填字段是否有值"""
    path = os.path.join(evidence_dir, filename)
    if not os.path.exists(path):
        return {"found": [], "missing": required_fields}

    data = load_json(path)
    found = []
    missing = []

    # Check in data section
    data_section = data.get('data', {})
    for field in required_fields:
        if isinstance(field, str):
            # Check if field exists and has value
            value = data_section.get(field)
            if value is not None and value != "" and value != [] and value != 0:
                found.append(field)
            else:
                missing.append(field)

    return {"found": found, "missing": missing}


def run_market_eval():
    """运行市场化生存评分，返回结果字典"""
    v6_dir = os.path.dirname(os.path.abspath(__file__))

    print("\n" + "=" * 60)
    print(" 市场化生存评分 (Market Viability Evaluation)")
    print("=" * 60)

    # 1. 加载评分标准
    rubric_path = os.path.join(v6_dir, 'rubrics', 'market_viability.json')
    rubric = load_json(rubric_path)
    print(f"  加载评分标准: {rubric['version']}")

    # 2. 加载证据文件
    evidence_dir = os.path.join(v6_dir, 'evidence')
    evidence_files = [f for f in os.listdir(evidence_dir) if f.endswith('.json')]
    print(f"  证据文件: {len(evidence_files)} 个")

    # 3. 按维度评分
    dimensions = rubric['dimensions']
    all_failures = []
    p0_failures = []
    p1_failures = []
    dim_scores = {}
    evidence_gaps = []

    for dim_key, dim_data in dimensions.items():
        dim_name = dim_data['name']
        dim_weight = dim_data['weight']
        print(f"\n── {dim_key}: {dim_name} (权重 {dim_weight}) ──")

        indicators = dim_data.get('indicators', [])
        dim_total_score = 0
        dim_max_score = 0

        for ind in indicators:
            ind_id = ind['id']
            ind_name = ind['name']
            ind_weight = ind.get('weight', 1)
            evidence_file = ind.get('evidence_file', '')
            required_fields = ind.get('required_fields', [])

            # 检查证据文件
            evidence_check = check_evidence_file(evidence_dir, evidence_file)
            field_check = check_required_fields(evidence_dir, evidence_file, required_fields)

            # 计算分数
            if not evidence_check['exists']:
                # 文件缺失 — 0分
                ind_score = 0
                evidence_gaps.append({
                    "indicator": f"{ind_id} {ind_name}",
                    "evidence_file": evidence_file,
                    "issue": "文件缺失",
                    "fix": f"创建 {evidence_file}"
                })
            elif evidence_check['evidence_level'] == 'placeholder':
                # 占位证据 — 基础分
                base_score = 30  # 文件存在就给30分
                # 必填字段有值的加分
                field_bonus = (len(field_check['found']) / max(1, len(required_fields))) * 30
                ind_score = min(evidence_check['score_cap'], base_score + field_bonus)

                evidence_gaps.append({
                    "indicator": f"{ind_id} {ind_name}",
                    "evidence_file": evidence_file,
                    "issue": f"占位证据 — 缺失字段: {field_check['missing']}",
                    "fix": f"收集真实数据填充 {evidence_file} 的缺失字段"
                })
            else:
                # 真实证据 — 按字段完成度
                field_ratio = len(field_check['found']) / max(1, len(required_fields))
                ind_score = min(evidence_check['score_cap'], 60 + field_ratio * 40)

            # 记录失败
            if ind_score < 60:
                failure = {
                    "indicator_id": ind_id,
                    "indicator": ind_name,
                    "dimension": dim_name,
                    "score": ind_score,
                    "threshold": 60,
                    "p_level": "P1",
                    "detail": f"市场化证据不足: {evidence_file} (得分 {ind_score:.0f}%)",
                    "fix_suggestion": f"补充 {evidence_file} 的真实市场证据",
                    "missing_fields": field_check.get('missing', []),
                    "evidence_level": evidence_check.get('evidence_level', 'unknown'),
                    "passed": False
                }
                all_failures.append(failure)
                if len(field_check.get('missing', [])) > len(field_check.get('found', [])):
                    p1_failures.append(f"{ind_id} {ind_name}")

            dim_total_score += ind_score * ind_weight
            dim_max_score += 100 * ind_weight

            status = "PASS" if ind_score >= 60 else "GAP"
            print(f"  [{status}] {ind_id} {ind_name}: {ind_score:.0f}% (证据: {evidence_check['evidence_level']})")

        # 维度汇总
        dim_score = (dim_total_score / max(1, dim_max_score)) * 100
        dim_scores[dim_key] = {
            "name": dim_name,
            "weight": dim_weight,
            "score": round(dim_score, 1)
        }
        print(f"  → {dim_name} 维度分: {dim_score:.1f}%")

    # 4. 计算总分
    total_score = sum(d['score'] * d['weight'] for d in dim_scores.values()) / 100

    # 5. 评级
    if total_score >= 90:
        grade = 'S (具备明确商业化潜力)'
    elif total_score >= 85:
        grade = 'A (有市场生存潜力)'
    elif total_score >= 75:
        grade = 'B (商业闭环不稳)'
    elif total_score >= 65:
        grade = 'C (更像演示项目)'
    else:
        grade = 'D (不具备独立市场生存能力)'

    # 6. 汇总
    placeholder_count = sum(1 for f in evidence_files
                          if load_json(os.path.join(evidence_dir, f)).get('evidence_level') == 'placeholder')
    real_count = sum(1 for f in evidence_files
                    if load_json(os.path.join(evidence_dir, f)).get('evidence_level') == 'real')

    print(f"\n  市场化生存总分: {total_score:.1f} ({grade})")
    print(f"  证据状态: {real_count} 真实 / {placeholder_count} 占位 / {len(evidence_files) - real_count - placeholder_count} 其他")
    print(f"  证据缺口: {len(evidence_gaps)}项")

    return {
        "total_score": total_score,
        "grade": grade,
        "dimensions": dim_scores,
        "failures": all_failures,
        "p0_failures": p0_failures,
        "p1_failures": p1_failures,
        "evidence_summary": {
            "total_files": len(evidence_files),
            "real_evidence": real_count,
            "placeholder_evidence": placeholder_count,
            "evidence_gaps": evidence_gaps
        }
    }
