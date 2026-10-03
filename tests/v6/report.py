#!/usr/bin/env python3
"""
v6 报告生成器 — 输出 JSON + MD + JSONL 三种格式。
应用 P0/P1 封顶规则。
"""

import json, os, time


class ReportGenerator:
    """合并系统质量评分 + 市场化评分，生成多格式报告"""

    def __init__(self, output_dir=None):
        if output_dir is None:
            output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                      'outputs')
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def generate(self, system_result, market_result):
        """生成所有报告"""
        # 1. 汇总
        combined = self._combine(system_result, market_result)

        # 2. 应用 P0/P1 封顶
        combined = self._apply_gates(combined)

        # 3. 生成 JSON
        json_path = os.path.join(self.output_dir, 'score-v6.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(combined, f, indent=2, ensure_ascii=False)
        print(f"  JSON报告: {json_path}")

        # 4. 生成 MD
        md_path = os.path.join(self.output_dir, 'score-v6.md')
        md_content = self._generate_md(combined)
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        print(f"  MD报告: {md_path}")

        # 5. 生成 JSONL
        jsonl_path = os.path.join(self.output_dir, 'failures-v6.jsonl')
        failure_count = self._generate_jsonl(combined, jsonl_path)
        print(f"  JSONL失败报告: {jsonl_path} ({failure_count}条)")

        return combined

    def _combine(self, system_result, market_result):
        """合并两套评分"""
        sys_dims = system_result.get('dimensions', {})
        mkt_dims = market_result.get('dimensions', {})

        sys_total = system_result.get('total_score', 0)
        mkt_total = market_result.get('total_score', 0)

        sys_failures = system_result.get('failures', [])
        mkt_failures = market_result.get('failures', [])

        sys_p0 = system_result.get('p0_failures', [])
        sys_p1 = system_result.get('p1_failures', [])
        mkt_p0 = market_result.get('p0_failures', [])
        mkt_p1 = market_result.get('p1_failures', [])

        all_p0 = sys_p0 + mkt_p0
        all_p1 = sys_p1 + mkt_p1
        all_failures = sys_failures + mkt_failures

        # 综合总分：系统质量80% + 市场化20%
        combined_total = sys_total * 0.80 + mkt_total * 0.20

        return {
            "version": "v6.0",
            "generated_at": time.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
            "framework": "NIST AI RMF + ISO/IEC 25010:2023 + RAGAS + FINRA 2026 + CFA Institute",
            "total_score": round(combined_total, 1),
            "system_quality_score": round(sys_total, 1),
            "market_viability_score": round(mkt_total, 1),
            "hard_gates": {
                "p0_failures": all_p0,
                "p1_failures": all_p1,
                "p0_cap_applied": len(all_p0) > 0,
                "p1_cap_applied": len(all_p1) > 0 and len(all_p0) == 0
            },
            "dimensions": {
                "system_quality": sys_dims,
                "market_viability": mkt_dims
            },
            "failures": all_failures,
            "top_issues": self._extract_top_issues(all_failures, n=10)
        }

    def _apply_gates(self, combined):
        """应用 P0/P1 封顶规则"""
        p0_failures = combined['hard_gates']['p0_failures']
        p1_failures = combined['hard_gates']['p1_failures']
        original_total = combined['total_score']
        original_sys = combined['system_quality_score']
        original_mkt = combined['market_viability_score']

        capped = False
        cap_reason = ""

        if len(p0_failures) > 0:
            # P0 失败 → 最多69
            capped_total = min(original_total, 69)
            capped_sys = min(original_sys, 69)
            capped_mkt = min(original_mkt, 69)
            capped = True
            cap_reason = f"P0失败{len(p0_failures)}项 → 封顶69"
        elif len(p1_failures) > 0:
            # P1 失败 → 最多85
            capped_total = min(original_total, 85)
            capped_sys = min(original_sys, 85)
            capped_mkt = min(original_mkt, 85)
            capped = True
            cap_reason = f"P1失败{len(p1_failures)}项 → 封顶85"
        else:
            capped_total = original_total
            capped_sys = original_sys
            capped_mkt = original_mkt

        combined['total_score'] = round(capped_total, 1)
        combined['system_quality_score'] = round(capped_sys, 1)
        combined['market_viability_score'] = round(capped_mkt, 1)
        combined['score_capped'] = capped
        combined['cap_reason'] = cap_reason
        combined['original_total'] = round(original_total, 1)

        return combined

    def _generate_md(self, combined):
        """生成 Markdown 报告"""
        lines = []
        lines.append("# 轻舟 v6 自动评分报告")
        lines.append(f"生成时间：{combined['generated_at']}")
        lines.append("")

        # 总分
        lines.append("## 总分")
        lines.append("")
        total = combined['total_score']
        grade = self._grade(total)
        lines.append(f"| 指标 | 分数 | 等级 |")
        lines.append(f"|---|---|---|")
        lines.append(f"| 综合总分 | **{total:.1f}** | **{grade}** |")
        lines.append(f"| 系统质量分 | {combined['system_quality_score']:.1f} | {self._grade(combined['system_quality_score'])} |")
        lines.append(f"| 市场化生存分 | {combined['market_viability_score']:.1f} | {self._grade(combined['market_viability_score'])} |")
        lines.append("")

        # 封顶状态
        gates = combined['hard_gates']
        if gates['p0_cap_applied']:
            lines.append(f"### P0 封顶：总分 ≤ 69")
            lines.append(f"P0 失败项：")
            for f in gates['p0_failures']:
                lines.append(f"- {f}")
            lines.append(f"原始总分：{combined.get('original_total', total):.1f}")
            lines.append("")
        elif gates['p1_cap_applied']:
            lines.append(f"### P1 封顶：总分 ≤ 85")
            lines.append(f"P1 失败项：")
            for f in gates['p1_failures']:
                lines.append(f"- {f}")
            lines.append(f"原始总分：{combined.get('original_total', total):.1f}")
            lines.append("")

        # 系统质量各维度
        lines.append("## 系统质量维度得分")
        lines.append("")
        lines.append("| 维度 | 得分 | 权重 | 加权 | 状态 |")
        lines.append("|---|---|---|---|---|")
        sys_dims = combined['dimensions'].get('system_quality', {})
        for dim_key, dim_data in sys_dims.items():
            name = dim_data.get('name', dim_key)
            score = dim_data.get('score', 0)
            weight = dim_data.get('weight', 0)
            weighted = score * weight / 100
            status = "PASS" if score >= 80 else "FAIL"
            lines.append(f"| {name} | {score:.0f} | {weight} | {weighted:.1f} | {status} |")
        lines.append("")

        # 市场化各维度
        lines.append("## 市场化生存维度得分")
        lines.append("")
        lines.append("| 维度 | 得分 | 权重 | 状态 |")
        lines.append("|---|---|---|---|")
        mkt_dims = combined['dimensions'].get('market_viability', {})
        for dim_key, dim_data in mkt_dims.items():
            name = dim_data.get('name', dim_key)
            score = dim_data.get('score', 0)
            weight = dim_data.get('weight', 0)
            status = "PASS" if score >= 60 else "EVIDENCE_GAP"
            lines.append(f"| {name} | {score:.0f} | {weight} | {status} |")
        lines.append("")

        # 前10问题
        lines.append("## 前10个最该修复的问题")
        lines.append("")
        for i, issue in enumerate(combined.get('top_issues', []), 1):
            p_label = issue.get('p_level', '?')
            lines.append(f"{i}. **[{p_label}]** {issue.get('indicator', '')}: {issue.get('detail', '')}")
            fix = issue.get('fix_suggestion', '')
            if fix:
                lines.append(f"   - 修复：{fix}")
            lines.append("")

        # 结论
        lines.append("## 结论")
        lines.append("")
        if total >= 92:
            lines.append("达到冲奖标准。继续优化以巩固分数。")
        elif total >= 85:
            lines.append("可提交参赛，但仍存在需要修复的 P1 问题。")
        elif total >= 70:
            lines.append("原型完整但智能体质量不足，需要重点修复合规与忠实度问题。")
        else:
            lines.append("不建议提交，需重构核心能力。")
        lines.append("")

        lines.append(f"*评分框架：{combined['framework']}*")
        return '\n'.join(lines)

    def _generate_jsonl(self, combined, path):
        """生成失败项 JSONL"""
        failures = combined.get('failures', [])
        count = 0
        with open(path, 'w', encoding='utf-8') as f:
            for failure in failures:
                f.write(json.dumps(failure, ensure_ascii=False) + '\n')
                count += 1
        return count

    def _extract_top_issues(self, failures, n=10):
        """提取前N个最该修复的问题（按P级别排序）"""
        priority = {'P0': 0, 'P1': 1, 'P2': 2, 'none': 3}
        # Only include failed items
        failed = [f for f in failures if not f.get('passed', True) or f.get('score', 100) < 80]
        failed.sort(key=lambda x: (priority.get(x.get('p_level', 'P2'), 3), x.get('score', 0)))
        return failed[:n]

    @staticmethod
    def _grade(score):
        if score >= 95:
            return 'S'
        elif score >= 92:
            return 'A+'
        elif score >= 88:
            return 'A'
        elif score >= 80:
            return 'B'
        elif score >= 70:
            return 'C'
        else:
            return 'D'
