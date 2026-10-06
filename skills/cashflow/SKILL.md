---
name: cashflow
description: 用户询问账单分类、异常或周期扣费、未来支出和可安排资金时，作为兼容入口选择财务诊断或目标规划流程。
metadata:
  role: advisor
  tools: [load_skill, statement_source, monthly_statement, annual_statement, anomalies, periodic_charges, account_summary, cashflow, budget, answer_facts, answer_reviewed]
  delivery: native
---

# 现金流与账单分析

## 选择流程

本技能保留旧入口。解释已发生收支、消费结构或异常时用 `load_skill` 加载 `financial-diagnosis`；问近期目标、留款底线、可花/可投金额时加载 `goal-planning`。订阅是否应取消继续加载 `subscriptions`，不把周期候选当商户合同。

1. 先查 `statement_source`，明确银行账单还是本地导入、覆盖期间及币种；读取对应 `monthly_statement` 或 `annual_statement`。未覆盖月份不补零，CSV结余不是银行余额，导入账单不覆盖模拟账户。
2. 异常与续费分别查 `anomalies`、`periodic_charges`；说明它们是规则候选，不能判定欺诈、重复扣款事实或已确认订阅。需要处理对象时再查业务记录。
3. 可安排金额先读 `account_summary`，再按所选账户调用 `budget`；近期压力用 `cashflow(days=...)`，只引用确定性结果。可安排金额与现金流 available 已扣应急底线/生效预留，不再扣一遍或与原底线比较；minimum_available 与零比较才表示底线余量/缺口，优先引用工具的底线是否被突破标志。分列已确认事项、候选和用户假设；未来未知工资及未记录日常支出不作确定输入。

## 输出与回退

说明期间、来源、消费或资金结论、证据、覆盖缺口及下一步。金融金额不由模型自行加减，退款、内部转账、借款与投资本金沿用工具口径；不同币种不合并。没有当前账户快照只能分析账单，不能由流水反推可用余额。没有完整输入则缩小结论范围，并说明需要核对什么；不以“暂无记录”推断没有消费。
