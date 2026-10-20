# 卡片分支核对

先读取 `business_records(kind='cards')` 的实际卡片、控制及申请。`prepare_operation` 每轮只准备一项；控制变更、挂失、申请均仍需审核与用户独立确认。

| 用户任务 | action与字段 | 条件与输出 |
|---|---|---|
| 网购/境外/非接触开关 | `card_control`；account_id、card_id、control_type、enabled | control_type为online/overseas/contactless；正常卡片且现有控制；不传limit_minor |
| 单笔限额 | `card_control`；account_id、card_id、control_type=single_limit、enabled=true、limit_minor | 只能调整正数整数分限额，不能关闭；信用卡单笔限额不超过当前信用额度 |
| 挂失 | `report_card_lost`；account_id、card_id | 仅正常卡片；成功回执才可说已模拟挂失 |
| 解挂 | `unblock_card`；account_id、card_id | 仅本人的已挂失卡；独立短信强验证后确认，未接通渠道不执行；解挂后控制保持禁用，不能当成另行开启支付 |
| 提额申请 | `request_card_limit`；account_id、card_id、requested_limit_minor | 仅正常信用卡；申请额高于当前额度，不低于未还金额；等待模拟审核，不承诺批准 |
| 办卡 | `apply_card`；account_id、requested_type、requested_limit_minor | requested_type为debit/credit；借记卡申请额度为0，信用卡为正数；只是申请 |
| 虚拟卡 | `apply_card`；account_id、requested_type=virtual、requested_limit_minor | 正数整数分额度，不传原card_id；展示用途、额度、费用、有效期和明确确认，只按回执说明是否已模拟核发 |
| 补卡 | `apply_card`；以上字段及原card_id | 原卡属于同一账户且已挂失；申请类型与原卡一致；不自行解除挂失或宣称新卡已核发 |

确认后按原 `operation_status` 查询；日期推进或后续模拟审核不由聊天自动触发。无法确认所属账户、状态或条款时停在必要澄清。已提交、待审核、失败和已生效分别说明。
