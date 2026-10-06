---
name: subscriptions
description: 用户检查重复或周期扣费、决定保留取消订阅、关闭银行代扣或追问取消结果时，区分候选、商户合同与银行授权并准备单项草案。
metadata:
  role: advisor
  tools: [load_skill, statement_source, periodic_charges, anomalies, account_summary, business_records, cashflow, prepare_operation, operation_status, withdraw_draft, answer_facts, answer_reviewed]
  delivery: native
---

# 订阅与代扣管理

## 工作方法

1. 用 `statement_source` 确认账单来源，按需用 `periodic_charges`、`anomalies` 查周期或重复扣费候选；历史频率和相似金额只支持待核实判断，不证明商户合同、欺诈或未来必扣。
2. 用 `account_summary` 和 `business_records(kind='subscriptions')` 核对所选账户的 subscriptions 与 mandates，分别读取商户状态、银行授权状态、金额、周期及关联对象。导入流水没有对应授权记录时说明无法查证，不按商户名称拼接ID。
3. 先理解用户是想省费、停服务、停止银行代扣，还是只查情况；结合本人实际使用需求、续费与取消条件比较保留/取消/先核实，不把消费行为自动当偏好。已有工具不提供合同取消费或服务剩余价值时标未知，不声称立即省下整期费用。
4. 办理分支按需通过 `load_skill` 加载 `name=subscriptions, reference=cancellation.md`。明确本轮请求且对象唯一时准备一项 `prepare_operation`；只询问、否定或撤回不准备。`answer_reviewed` 引用实际草案，等本人独立确认。

## 输出与回退

按“周期候选—商户合同—银行授权”分别报告依据和当前状态。取消商户订阅不自动关闭银行代扣，关闭银行代扣不解除商户合同或免除付款责任；金额候选不纳入确定承诺。影响近期资金时可读 `cashflow`，不由模型计算新余额。

确认后按原ID用 `operation_status` 查回执，再读取双方状态；复合取消请求先办一项、查实际回执再继续。未知结果不能重复创建操作；旧草案撤回用 `withdraw_draft`。所有变更仅为本机合成账本，不联系真实商户。无对应合同或权限时说明缺口及核查办法，不以关闭另一项代替用户原要求。
