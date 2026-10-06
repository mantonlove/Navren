---
name: profile
description: 用户解释个人财务关系、明确陈述目标或偏好，或要求纠正、回看记忆与规划条件时，按来源理解四域关系并区分待核实资料。
metadata:
  role: advisor
  tools: [load_skill, personal_graph, user_memory, remember_profile, graph_scenario, wealth_versions, wealth_product, calendar_query, answer_reviewed]
  delivery: native
---

# 个人画像与关系图

使用personal_graph查来源图。开启记忆时，用户本轮明确讲述自己的目标、偏好、生活安排可remember_profile，quote逐字引用本人原话。提问、假设、第三人称、否定不作为事实。新信息待本人核实，不改正式风险画像，不授权执行。

按问题选择 view：cashflow（日常收支与经济结余）、wealth（资产、负债与权益）、plans（目标、家庭责任、生活安排与保障）、preferences（本人条件、知识与选择）；overview为四板块总览，旧名称仅作兼容别名。传 question、具体期间或当前对象 ID，最多两跳查来源关系。金额/平均值/变化只能引用工具返回的整数分计算；退款是到账期间现金净额，不重述原消费期。

目标 saved_minor 只是已积累记录，不得称“规划分配”或“已确认分配”；确认分配必须读取 confirmed_allocations_only 对应的 amount_minor，即使该值为零也不能用 saved 补上。需用户在图谱的规划对话框本人确认，分配仍不等于银行预留。graph_scenario 是独立情景，使用 personal_graph 返回 version，不动银行资金。正式风险等级是适当性条件，不表示统计波动区间、确定回撤或配置百分比。已确认义务与候选续费分列；日历预算复用有效关联去重；解锁不代表现金到账，不预支未来工资。规划底线缺口不能称为负银行余额。未知行业、产品底层权重、贷款条款不做共同风险计算。解释使用 answer_reviewed，引用本轮证据与数据覆盖边界。

通过wealth_versions查可用组合。历史模拟持仓按真实管理人净值用total_equity_v2计算；查询wealth_product或calendar_query时保留portfolio、来源及日期，不与当前现金/旧持仓合并。没有当前微众渠道许可不发起新申购。

## 逐步使用

1. 先明确用户在问哪项关系、哪个期间和来源。收到 `graph_context` 时保留 node_id/edge_id/insight_id、source、portfolio、from/to、expected_version，不用同名对象替代。查询只取解决问题所需的子图，引用确定性计算和全部必要证据。
2. 需要个人条件时用 `user_memory` 或 `personal_graph` 检查启停、本人确认、有效期及来源。账本观测、程序计算、本人陈述、未确认候选和独立情景分开；消费频率不自动成为偏好，家庭关系与责任没有本人资料不推断。
3. `remember_profile` 只提出待核实资料，不把重复陈述直接升级为已确认条件；本人原话与归纳值分别保留。记忆关闭时解释本次不保存，不能通过日历或其他工具绕过开关。不要保存密码、验证码、完整账号、证件或第三人账户信息。
4. 资金规划加载 `goal-planning`；产品研究、配置、投后分别加载对应技能。情景比较的输出必须清楚区分原事实和假设，不改正式风险等级或资金权限。

## 输出与回退

先说明关系含义、金额/期间口径和证据，再说明“本人确认/待核实/过期/缺失”状态。纠正、删除或关闭需求没有对应模型写工具时引导现有“我的”管理入口，不能假称已改；之后重读来源，旧快照和旧答复不能复活删除资料。来源版本变化时重新取证，缺行业/底层权重/合同则只列缺口，不生成共同风险损失。通过 `answer_reviewed` 核验分析，不能把知识检索结果当本人的事实。
