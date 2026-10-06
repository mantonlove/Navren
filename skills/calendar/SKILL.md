---
name: calendar
description: 用户查看每日收支、待付事项、理财损益或明确创建修改删除生活事件时，读取共享财务日历并准备可确认的事件草案。
metadata:
  role: advisor
  tools: [calendar_query, calendar_day, calendar_item, calendar_budget_scenario, calendar_prepare_event, account_summary, business_records, wealth_versions, prepare_operation, operation_status, answer_facts, answer_reviewed]
  delivery: native
---

# 财务日历与生活安排

## 已有数据与确认约束

使用 calendar_query 获取同一账户来源的日历，period 可表达相对业务期间；from/to 必须同时明确，不与 period 混用。每次最多93天。查询产生组件，用终结工具 widget_ids 引用；不是返回模型编写的HTML。
已记录收支、确认待付、候选续费、用户预算、理财损益分别解释。无记录不等于零消费。收益缺净值、持仓数量或完整交易时不可计算，本金解锁不等于现金到账。导入流水与模拟账户独立。
日详情使用 calendar_day，分页后 has_more 时继续请求或明确未展示全部。事项用 calendar_item 读取最新状态，不凭历史文字覆盖源记录。
用户明确创建/编辑/删除事件时使用 calendar_prepare_event，只准备草案。周期编辑必须明确 occurrence/future/series；单次事件默认 series。日期、农历、公历、月末或金额有歧义就澄清。只支持公历；月底 last_day 和跳过 skip 必须在确认卡呈现。
金融操作从最新事项查对应业务对象，经过原有准备、审核及用户确认；元数据预算/备注不能授权转账，不能新建未经核验的收款人。每轮至多一项金融草案。
记忆关闭时仍可读用户主动保存的日历；不读取被禁用的画像。只用必要称呼、日期，不采集第三人账户身份。生日预算不自动纳入预留，也不扣款。
用户明确要把日历计划预算纳入资金规划时，先读取账户，再使用 calendar_budget_scenario。它把同币种事件的计划预算与已计入承诺/生效预留按有效引用去重；只是情景计算，不改变可用余额、资金预留或交易权限。预算没有有效关联时不猜已支付；需要保留资金或支付仍准备单独业务草案。

## 任务流程与输出

1. 查询先确定期间、账单来源、组合及时间视图。相对期间由 `period` 交给后端业务时钟；真实基金收益先 `wealth_versions` 取得组合身份，再查询对应组合，保留净值截至日期，不把模拟业务日当市场今天。
2. 指定日期/事项先读取实际 view_id/item_id。旧卡是当时快照，详情读最新来源；跨账户快照不展示。修改/删除先查真实 series_id 和当前状态，不以创建重复事件替代；同名多项只澄清必要区别。
3. 准备事件前核对公历日期、类型、重复规则、无效日期策略、编辑范围、可选预算与提醒。只引用本轮成功 `calendar_prepare_event` 的确认组件；聊天文字“确认”不保存。读取 server_context 的 calendar_events、calendar_draft_receipts 核对是否已确认，不被旧助手文字覆盖。
4. 通过终结工具引用本轮 `widget_ids`，正文用两三句话说明期间、来源、关键结论和限制，不逐日复写组件。日期详情未分页完整时说明剩余项；每笔事实保持对应来源。

## 缺数与失败

无记录、未披露、部分缺失和服务失败分开说明；未披露不填零。草案过期或来源变化重新读取、重新准备，不能展示旧确认权限。站内提醒只在应用打开或查询时检查，不能承诺后台推送、设备日历或真实扣款。事件预算和手工完成状态不是银行支付事实；用户计划情景不会冻结资金。
