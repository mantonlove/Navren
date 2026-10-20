# 收付款分支核对

以下均通过现有 `prepare_operation` 准备，不提供确认或执行权限。先读取当前 `account_summary` 和 `business_records(kind='payments')`。金额单位为整数分，ID来自本轮工具；不要传幂等键或验证码。

| 用户任务 | action与必需业务字段 | 核对要点 |
|---|---|---|
| 即时转账 | `transfer`；account_id、beneficiary_id、amount_minor | 已核验收款人；当前模拟转账只支持人民币；后端核对账本余额、实际冻结/预留和限额；应急目标与预计账单为提示，本轮明确的留款规划约束仍需遵守 |
| 定时转账 | `schedule_transfer`；account_id、beneficiary_id、amount_minor、frequency、next_due_on | frequency为once/monthly；首次日期不早于业务日；end_on仅每月计划可选；日期推进到期才执行 |
| 取消计划 | `cancel_scheduled_payment`；account_id、scheduled_payment_id | 计划属于该账户且仍active；不取消已发生转账 |
| 提交AA请求 | `request_split`；account_id、split_id | 只处理已有draft账单；分摊份额由后端核对，不凭聊天新建账单或参与人 |
| 登记AA收款 | `collect_split_payment`；account_id、split_id、participant_id | 账单仍open/partially_paid，参与人有requested份额；按已有未收份额登记，不能虚构外部到账 |

用户修改对象或金额先重读记录再准备替代草案；撤回旧卡用 `withdraw_draft`。复合请求拆成逐项草案、本人确认、原回执查询，再继续。用户说“已收到”是办理请求的数据，不是真实银行外部到账证据；结果必须清楚标明模拟登记。
