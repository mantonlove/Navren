#!/usr/bin/env python3
"""
v6 系统质量评分编排。
加载 rubrics → 加载 cases → 运行 validators → 调用 llm_judge → 汇总。
"""

import json, os, sys, time

# Ensure we're in the project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from agent_core import QingzhouAgent
from tests.v6.validators import ValidatorRegistry, CheckResult
from tests.v6.llm_judge import LLMJudge


def load_jsonl(path):
    """加载 JSONL 文件，返回 list of dicts"""
    cases = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        cases.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return cases


def load_json(path):
    """加载 JSON 文件"""
    with open(path) as f:
        return json.load(f)


def run_system_eval():
    """运行系统质量评分，返回结果字典"""
    v6_dir = os.path.dirname(os.path.abspath(__file__))

    print("\n" + "=" * 60)
    print(" 系统质量评分 (System Quality Evaluation)")
    print("=" * 60)

    # 1. 加载评分标准
    rubric_path = os.path.join(v6_dir, 'rubrics', 'system_quality.json')
    rubric = load_json(rubric_path)
    print(f"  加载评分标准: {rubric['version']} — {rubric['framework']}")

    # 2. 初始化 Agent 和校验器
    print("  初始化 QingzhouAgent ...")
    t0 = time.time()
    agent = QingzhouAgent()
    print(f"  产品库加载: {len(agent.products)} 只在售产品 ({time.time()-t0:.1f}s)")

    validator = ValidatorRegistry(agent)
    llm_judge = LLMJudge()
    if llm_judge.available:
        print(f"  LLM裁判: {llm_judge.judge_model} (风格/解释质量)")
        from tests.v6.llm_judge import RERANK_MODEL
        print(f"  Reranker: {RERANK_MODEL} (相关性打分)")
    else:
        print("  LLM裁判: 不可用 (YUANJING_API_KEY not set) — 主观指标将降级为规则评分")

    # 3. 加载测试用例
    cases_dir = os.path.join(v6_dir, 'cases')
    all_cases = {}
    case_files = {
        'compliance': 'compliance_cases.jsonl',
        'suitability': 'suitability_cases.jsonl',
        'rag': 'rag_faithfulness_cases.jsonl',
        'followup': 'followup_cases.jsonl',
        'persona': 'persona_cases.jsonl',
        'market_linkage': 'market_cases.jsonl',
    }
    for key, fname in case_files.items():
        path = os.path.join(cases_dir, fname)
        all_cases[key] = load_jsonl(path)
        print(f"  用例 {key}: {len(all_cases[key])} 条")

    # 4. 按维度运行所有指标
    dimensions = rubric['dimensions']
    all_failures = []
    p0_failures = []
    p1_failures = []
    dim_scores = {}

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
            check_type = ind.get('check_type', 'deterministic')
            p_level = ind.get('p_level', 'P2')

            # 找到匹配的用例
            relevant_cases = []
            if ind_id.startswith('A'):
                relevant_cases = all_cases.get('compliance', []) + all_cases.get('suitability', [])
            elif ind_id.startswith('B'):
                relevant_cases = all_cases.get('rag', [])
            elif ind_id.startswith(('C', 'D')):
                relevant_cases = all_cases.get('suitability', [])
            elif ind_id.startswith('E'):
                relevant_cases = all_cases.get('persona', [])
            elif ind_id.startswith('F'):
                relevant_cases = all_cases.get('followup', []) + all_cases.get('market_linkage', [])
            elif ind_id.startswith(('G', 'H', 'I', 'J')):
                relevant_cases = []  # 基础设施检查不需要用例

            # 确定性检查或LLM裁判
            if check_type == 'deterministic' or check_type == 'hybrid':
                # 运行确定性校验
                before_count = len(validator.results)
                validator.run_indicator(ind, relevant_cases, dim_name)
                new_results = validator.results[before_count:]

                # 从新结果中提取分数
                if new_results:
                    # 用汇总结果（最后一个通常是总结）
                    summary = new_results[-1]
                    if summary.check_id == ind_id:
                        ind_score = summary.score
                    else:
                        # 取平均
                        scores = [r.score for r in new_results if r.check_id == ind_id]
                        ind_score = sum(scores) / max(1, len(scores)) if scores else 50
                else:
                    ind_score = 50

            elif check_type == 'llm_judge':
                # 使用LLM裁判
                func_name = ind.get('check_function', '')
                judge_method = getattr(llm_judge, func_name, None)

                if judge_method and llm_judge.available:
                    scores = []
                    # 根据指标选择用例和调用方式
                    if ind_id.startswith('E'):
                        # 三模式风格评判 — 用 persona_cases
                        judge_cases = all_cases.get('persona', [])
                        for case in judge_cases:
                            if case.get('check_style') or case.get('check_persona'):
                                modes = case.get('modes', [case.get('mode', 'classic')])
                                for mode in modes:
                                    result = agent.process(case['input_text'],
                                                         profile=case.get('profile', {}),
                                                         mode=mode)
                                    reply = result.get('reply', '')
                                    jr = judge_method(reply)
                                    scores.append(jr.get('score', 50))
                    elif ind_id == 'B4':
                        # 答案相关性 — 用 rag + suitability cases
                        judge_cases = all_cases.get('rag', []) + all_cases.get('suitability', [])
                        for case in judge_cases[:6]:  # 限制数量
                            result = agent.process(case['input_text'],
                                                 profile=case.get('profile', {}),
                                                 mode=case.get('mode', 'classic'))
                            reply = result.get('reply', '')
                            jr = judge_method(case['input_text'], reply)
                            scores.append(jr.get('score', 50))
                    elif ind_id == 'D3':
                        # 澄清问题效率
                        judge_cases = all_cases.get('suitability', [])
                        for case in judge_cases[:4]:
                            result = agent.process(case['input_text'],
                                                 profile=case.get('profile', {}),
                                                 mode=case.get('mode', 'classic'))
                            reply = result.get('reply', '')
                            jr = judge_method(case['input_text'], reply)
                            scores.append(jr.get('score', 50))
                    else:
                        scores = [60]  # fallback

                    ind_score = sum(scores) / max(1, len(scores)) if scores else 60
                else:
                    # 降级：默认及格分
                    ind_score = 60
                    validator.results.append(CheckResult(
                        ind_id, dim_name, ind_name,
                        True, 60,
                        detail="LLM裁判不可用，降级为规则评分",
                        fix_suggestion="设置YUANJING_API_KEY环境变量以启用LLM裁判",
                        p_level=p_level
                    ))

            # 标记失败
            if ind_score < ind.get('threshold', 80):
                failure = {
                    "indicator_id": ind_id,
                    "indicator": ind_name,
                    "dimension": dim_name,
                    "score": ind_score,
                    "threshold": ind.get('threshold', 80),
                    "p_level": p_level,
                    "detail": f"{ind_name}得分 {ind_score:.0f}% < 阈值 {ind.get('threshold', 80)}%",
                    "fix_suggestion": ind.get('fix_suggestion_template', f'提升{ind_name}至{ind.get("threshold", 80)}%以上'),
                    "passed": False
                }
                all_failures.append(failure)
                if p_level == 'P0':
                    p0_failures.append(f"{ind_id} {ind_name}")
                elif p_level == 'P1':
                    p1_failures.append(f"{ind_id} {ind_name}")

            dim_total_score += ind_score * ind_weight
            dim_max_score += 100 * ind_weight

            status = "PASS" if ind_score >= ind.get('threshold', 80) else "FAIL"
            print(f"  [{status}] {ind_id} {ind_name}: {ind_score:.0f}% (阈值{ind.get('threshold', 80)}%)")

        # 维度汇总
        dim_score = (dim_total_score / max(1, dim_max_score)) * 100
        dim_scores[dim_key] = {
            "name": dim_name,
            "weight": dim_weight,
            "score": round(dim_score, 1),
            "pass": dim_score >= dim_data.get('min_pass_score', 80)
        }
        print(f"  → {dim_name} 维度分: {dim_score:.1f}% (最低要求: {dim_data.get('min_pass_score', 80)}%)")

    # 5. 汇总失败项（从 validator.results 收集详细失败信息）
    for r in validator.results:
        if not r.passed or r.score < 70:
            all_failures.append(r.to_dict())

    # 6. 计算总分
    total_score = sum(d['score'] * d['weight'] for d in dim_scores.values()) / 100

    # 7. 评级
    if total_score >= 95:
        grade = 'S'
    elif total_score >= 92:
        grade = 'A+'
    elif total_score >= 88:
        grade = 'A'
    elif total_score >= 80:
        grade = 'B'
    elif total_score >= 70:
        grade = 'C'
    else:
        grade = 'D'

    print(f"\n  系统质量总分: {total_score:.1f} ({grade})")
    print(f"  P0失败: {len(p0_failures)}项, P1失败: {len(p1_failures)}项")
    print(f"  失败项总计: {len(all_failures)}条")
    if llm_judge.skipped_count > 0:
        print(f"  LLM裁判跳过: {llm_judge.skipped_count}次 (无API Key)")

    return {
        "total_score": total_score,
        "grade": grade,
        "dimensions": dim_scores,
        "failures": all_failures,
        "p0_failures": p0_failures,
        "p1_failures": p1_failures,
        "llm_judge_skipped": llm_judge.skipped_count,
        "validator_results_count": len(validator.results)
    }
