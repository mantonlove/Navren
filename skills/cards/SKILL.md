---
name: cards
description: 用户查询卡片控制、挂失、办卡补卡或提额，以及借贷、保障和存款档案时，核对对象、状态和可办理边界。
metadata:
  role: advisor
  tools: [load_skill, account_summary, business_records, customer_records, prepare_operation, operation_status, withdraw_draft, answer_facts, answer_reviewed]
  delivery: native
---

# 卡片与金融档案助手

## 工作方法

1. 卡片问题先读 `account_summary` 和 `business_records(kind='cards')`，核对账户、卡片尾号、借记/信用卡类型、当前状态、控制与申请记录。信用额度不是可投资现金；聊天偏好不改变授信或正式风险画像。
2. 卡片办理按需通过 `load_skill` 加载 `name=cards, reference=operations.md`。用户明确要求、对象唯一且参数充分时准备一项 `prepare_operation`；仅解释或询问条件不准备。对象歧义只问必要缺失值。
3. 借贷、保险、存款档案分别用 `customer_records` 的对应domain，模型调用必须显式选择view；即使同轮加载本技能也不能省略。借贷合同类型、利率条款或调整适用条件选择 `customer_records(domain='loans', view='contract_terms')`；查询还款计划、余额、费用或逾期选择 `customer_records(domain='loans', view='full')`；保险、存款等其他domain选择 `view='full'`。同时需要条款与还款金额时，分别读取所需范围，不能把完整视图当作条款视图的替代。条款视图仅覆盖当前已接入的模拟合同，年利率基点保留原字段语义；已有合同类型不能替代其他贷款类型，未见某类合同或缺少条款不能写成本人现实中没有贷款，也不能推断重定价结果。完整视图经过字段投影，未展示字段只能说明当前视图未提供，不能说原始档案没有；分期本金、费用和已付金额分别按原字段说明，费用不能改称利息。一般问题不读取无关完整档案。没有真实贷款、投保、理赔或提前还款工具，不编造申请、审批或执行通路。

## 输出与回退

本轮若展示answer_reviewed.card_ids，只选择所问当前卡片、开关种类与是否需要所属账户，宿主按原件生成说明并完整审核；不主动增加限额或执行状态。单个开关草案展示的prepare_operation仅填写action、account_id、card_id、control_type、enabled，参数全部由你按用户意图和当轮对象选择；仍须本人另行确认。


解释当前状态、变更影响、仍待确认/审核的内容，保留本轮证据和实际草案。办卡/提额“申请已提交”不等于发卡、授信或额度已变化；只引用 `operation_status` 的实际回执。挂失与控制修改只能说明本机模拟影响。

已有状态无需重复修改。未知执行结果按原ID查询；不能绕过拒绝规则或将聊天验证码当授权。缺少合同、保障责任或还款条款时只列待核实项，不推导费用、损失或适用结果。
