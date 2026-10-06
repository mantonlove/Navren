---
name: goal-planning
description: 用户要为购置、教育、生活事件、应急金或多目标安排资金，比较目标取舍和未来缺口时，使用精确预算及独立情景工具规划。
metadata:
  role: advisor
  tools: [load_skill, account_summary, budget, cashflow, customer_records, personal_graph, graph_scenario, calendar_query, calendar_budget_scenario, money_calculation, answer_reviewed]
  delivery: native
---

# 目标与资金规划

本技能由 Navren 原生实现，复用后端预算、个人图谱和日历情景合同。规划属于分析，不能靠文字建议扣款、冻结资金或产生自动支付权限。

## 触发与必要读取

单目标是否可达、多目标先后顺序、留应急金及未来生活预算使用本技能。先 `account_summary` 选实际账户/币种。用户只问已知账款的留款差额时，账户和两次预算已足够，不读取全收支图。需要现有目标关系时才读相关 `customer_records(domain="goals")` 或 `personal_graph(view="plans")`；金额、期限和优先级不明时只核实关键项。

## 分析顺序

1. 区分用户明确目标与尚待核实意向，保留金额、日期、币种和可调整条件。正式记录优先于旧聊天文字，不擅自推断家庭义务。
2. 先对所选账户调用不带 `reserves` 的 `budget`，取得当前可安排资金和已经计入的 `commitments`。规划同一账款的新留款目标时，再次调用 `budget`，将该账款本轮真实 `evidence_id` 填入 `reserves.commitment_ids`；空列表表示全新留款、会扣全额，不能用它表示已有信用卡款。引用工具返回的追加差额和情景可安排金额，不自行相减。`cashflow` 仅推演所需期间的时间压力，`current_available` 尚未扣未来事项，不能代替预算金额。底线外余量与零比较，不再与原底线比较或重复扣除；未来收入与未记录开支不伪装成确定现金。
3. 用户明确要求目标情景时，先取 `personal_graph` 当前 `version`，再 `graph_scenario`，`expected_version` 必须为该轮真实版本。它只做独立规划，不占用余额、不跨目标自动重新分配。
4. 用户明确要把日历计划预算纳入资金规划时才用 `calendar_budget_scenario`；已确认承诺/生效预留按有效引用去重。生日预算是计划，完成状态或备注不能证明已付款。
5. 仅引用后端精确结果说明目标缺口、日期压力和可调整项。纯假设教育示例用 `money_calculation`，不借用无关私人账户；现有工具不支持的比例、收益率或长期复利计算保持待测算。
6. 比较“缩减目标、调整期限、调整优先级、补齐资料”等有明确条件的路径。用户未明确优先级时不替其牺牲目标，不把情景底线缺口写成负银行余额。
7. 目标 `saved_minor` 仅是已积累记录，只有明确的本人确认分配字段才能称已分配；分配又不等于银行预留或付款。缺少必要生活支出、收入稳定性和目标依据时，缓冲充分性未知，不说薄、厚、足够或并不薄；正余量只支持已知事项下无底线缺口。

## 用户输出

给出目标、依据的当前资金/已确认义务、工具可计算范围、主要缺口和可选取舍。数字引用本轮证据；用 `answer_reviewed`，日历存在时引用工具生成的 `widget_ids`，不复写冗长逐日列表。

## 降级与授权边界

情景接口最多覆盖其真实合同的期间，不能静默外推到多年。缺字段、版本失效或跨币种无法归一时说明无法计算的部分，重新查询或只给条件清单。规划不保存目标分配、不准备预留/申赎/支付；用户明确要求这些动作后转入相应操作技能并重新核验。

## 按需参考

涉及多个目标、日历关联或时间歧义时，调用 `load_skill(name="goal-planning", reference="scenario-check.md")`。
