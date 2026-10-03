#!/usr/bin/env python3
"""
轻舟 v6 自动评分体系 — 主入口。
运行方式：python3 tests/v6/run_all.py
输出：outputs/score-v6.json, outputs/score-v6.md, outputs/failures-v6.jsonl
"""

import sys, os, time

# Ensure we're in the project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from tests.v6.run_system_eval import run_system_eval
from tests.v6.run_market_eval import run_market_eval
from tests.v6.report import ReportGenerator


def main():
    start_time = time.time()

    print("=" * 60)
    print("  轻舟 v6 自动评分体系")
    print("  框架: NIST AI RMF + ISO/IEC 25010 + RAGAS + FINRA 2026")
    print("=" * 60)

    # 1. 系统质量评分
    system_result = run_system_eval()

    # 2. 市场化生存评分
    market_result = run_market_eval()

    # 3. 生成报告
    print("\n" + "=" * 60)
    print(" 生成评分报告")
    print("=" * 60)

    report = ReportGenerator()
    combined = report.generate(system_result, market_result)

    # 4. 最终输出
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print(" 评分完成")
    print("=" * 60)

    total = combined['total_score']
    sys_score = combined['system_quality_score']
    mkt_score = combined['market_viability_score']

    gates = combined['hard_gates']
    print(f"  综合总分: {total:.1f}")
    print(f"  系统质量: {sys_score:.1f}")
    print(f"  市场化生存: {mkt_score:.1f}")

    if gates['p0_cap_applied']:
        print(f"  P0封顶: 是 (总分≤69)")
        print(f"  原始总分: {combined.get('original_total', total):.1f}")
    elif gates['p1_cap_applied']:
        print(f"  P1封顶: 是 (总分≤85)")
        print(f"  原始总分: {combined.get('original_total', total):.1f}")

    print(f"  P0失败: {len(gates['p0_failures'])}项")
    for f in gates['p0_failures']:
        print(f"    - {f}")
    print(f"  P1失败: {len(gates['p1_failures'])}项")
    for f in gates['p1_failures'][:5]:
        print(f"    - {f}")
    if len(gates['p1_failures']) > 5:
        print(f"    ... 及其他 {len(gates['p1_failures']) - 5} 项")

    print(f"  失败项总计: {len(combined['failures'])}条")
    print(f"  耗时: {elapsed:.0f}s")

    print(f"\n  报告文件:")
    print(f"    outputs/score-v6.json")
    print(f"    outputs/score-v6.md")
    print(f"    outputs/failures-v6.jsonl")

    # 前10问题
    top = combined.get('top_issues', [])[:10]
    if top:
        print(f"\n  前{len(top)}个最该修复的问题:")
        for i, issue in enumerate(top, 1):
            dim = issue.get('dimension', '')
            ind = issue.get('indicator', issue.get('indicator_id', ''))
            detail = issue.get('detail', '')
            fix = issue.get('fix_suggestion', '')
            print(f"  {i}. [{issue.get('p_level', '?')}] {dim}/{ind}: {detail[:80]}")
            if fix:
                print(f"     → {fix[:100]}")

    return combined


if __name__ == '__main__':
    main()
