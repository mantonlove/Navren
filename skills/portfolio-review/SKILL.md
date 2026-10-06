---
name: portfolio-review
description: 用户查看持仓收益、组合变化、目标偏离、集中风险或是否需要调整时，先核对组合版本与权益完整性，再基于已确认目标做只读投后复核。
metadata:
  role: advisor
  tools: [load_skill, account_summary, business_records, wealth_versions, wealth_product, calendar_query, personal_graph, budget, cashflow, operation_status, answer_reviewed]
  delivery: adapted
---

# 组合与投后复核

方法适配自 `anthropics/claude-for-financial-advisors` 的 `portfolio-rebalance-review`：保留先核查持仓质量、再比较已确认目标、说明偏离与调整理由。版本/许可由 `skills/upstream/provenance.json` 登记；不继承美国税务、外部账户连接器或默认再平衡阈值。

## 触发与必要读取

收益核查、持仓变化、风险集中、配置偏离或调整评估使用本技能。先 `wealth_versions` 明确组合身份、活动/归档、来源与覆盖；查当前持仓时用 `account_summary` 和必要 `business_records(kind="investments")`。历史收益用明确的 `wealth_product` 或 `calendar_query`，传同一 `portfolio`、期间和核算口径。

## 分析顺序

1. 分开当前模拟本金持仓、真实净值支持的独立历史模拟组合、归档版本和用户未接入外部持仓。每只 1000 份历史示例不是用户历史持仓，也不与当前现金/旧持仓合并。
2. 检查估值日期、份额、成本、交易/费用/分红/企业行为、账户与基金完整性及重复计数。缺一个预期账户/份额时整体金额未知，已知部分另列；不静默删掉缺项。
3. 引用工具按 `total_equity_v2` 或明确的旧口径计算的权益/当期损益/累计损益，说明各自期间。应收、待确认权益与到账转换不能重复计收益；首日缺期初值或假日无净值不补零。
4. 只有用户已确认目标权重/区间与有效来源时才能比较偏离；否则列当前已知结构与待确认目标，不选默认模型，不自定百分比阈值。现有工具未返回偏离数值时不自行计算。
5. 用 `personal_graph`、`cashflow` 和 `budget` 分析实际条款下的流动性/目标影响；未知底层权重或行业不估损失。历史回放不证明当前配置改善用户收益。
6. 用户询问调整时解释继续持有、补充研究或重新规划的条件。已有执行状态用 `operation_status` 查真实回执，不凭聊天、本人承诺或截图标记执行完成。

## 用户输出

先给核算可用性与关键发现，注明组合、截至日、期间和资料缺口，再给附条件的投后观察/复核事项。用 `answer_reviewed` 引用本轮证据；日历组件引用工具 `widget_ids`。正常答复少用内部版本 ID，但必要身份不能省略到混淆范围。

## 降级与授权边界

市值、成本或交易链缺失时可解释已知持仓本金与退出条款，不编每日收益或亏损。归档仅回看，不能作为当前交易依据。调整建议不执行赎回/购买、不改默认组合、不开启后台监控或自动再平衡；具体操作转入业务技能，重新查当前状态并交用户确认。

## 按需参考

收益与配置偏离混用或多账户资料不齐时，调用 `load_skill(name="portfolio-review", reference="portfolio-check.md")`。
