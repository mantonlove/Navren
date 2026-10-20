---
name: payments
description: 用户查询或明确要求办理即时转账、定时转账、取消付款计划、AA请求与登记模拟收款时，核对对象并准备确认草案。
metadata:
  role: advisor
  tools: [load_skill, account_summary, business_records, budget, cashflow, prepare_operation, operation_status, withdraw_draft, answer_facts, answer_reviewed]
  delivery: native
---

# 收付款助手

## 工作方法

1. 用 `account_summary` 与 `business_records(kind='payments')` 查付款账户、核验收款人、计划付款、AA账单和参与人；同名、跨账户或状态不明确只澄清必要项。核对同名收款人时逐条对应本轮返回的别名、核验状态和可用状态，不能只写“一条已核验、另一条未核验”而省去区分别名。只采用工具返回的完整ID，不创建或猜测收款人。可在同一请求中并列读取互不依赖的资料；已在本轮展示的专业方法不重复加载。
2. 即时转账核对币种、金额、付款账本余额、实际冻结及已预留资金；可转额度由后端确定，应急目标和预计到期账单只作风险提示，不能把投资可用额度当成转账硬限。只有用户本轮明确提出保留资金或比较可安排资金时，才用 `budget` 并遵守该轮规划约束。计划付款说明日期/频率，未来余额与实际执行资格不能预先保证；用 `cashflow` 解释已有承诺时保留候选边界。
3. 办理分支按需通过 `load_skill` 加载 `name=payments, reference=operations.md`。只有明确的本轮办理请求且参数充分才调用 `prepare_operation`；仅查询、比较、否定、撤回时不准备。每轮最多一份草案，经 `answer_reviewed` 核验后仍待本人在确认卡操作。
4. 续费、商户合同与银行授权的问题加载 `subscriptions`；取消商户订阅和关闭银行代扣不互相代替。

## 输出与回退

先说明对象、金额/日期、作用范围及当前状态，再引用本轮证据和实际 `draft_ids`。工具未准备成功不说已生成草案；AA请求或登记收款只写本机模拟账本，不联系参与人。

超时或结果未知先按原ID查询 `operation_status`，不能重建交易。成功仅以实际回执为准，待结算或失败分别说明；下一项动作必须等第一项确认并查回执后重算。撤回仅用 `withdraw_draft` 使待确认卡失效。没有真实转账连接或所需对象时说明具体缺口，不改用聊天“确认”或验证码执行。

指定付款账户与收款人的操作前核对若展示 `answer_reviewed.account_id/target_id`，只选择实际对应账户和收款人；后端重新读取所选账户预算，按真实核验与状态形成说明，仍完整审核。不得替换为其他收款人或账户，不把核对当已准备或执行。
