# Navren 消息组件初版实施方案

2026-10-07。用户已批准“你先做着试试看吧”。本轮落实聊天实际消费者与回复生产端，不改变账户、我的、发现及导航的既有设计。无需新建会话、整套验收或新增润色/测试模型调用。

## 初版实施范围

接通纯事实回复的受控展示投影；首批支持：紧凑关键数字、逐笔流水、现金消费分类条形、资金扣减明细、账户资金列表。年度账单可展示已有汇总，但底层暂无逐月数值序列，不生成年度趋势图。审核回复暂时保留原审核正文与既有日历，完整审核组件合同另行接通。单条答案以一个主要组件为核心。条形长度只是绘图几何，不在前端创造占比、合计、余额、收益或可安排资金。

普通查询直接回答后结束。分析先给一句主要结论，最多两三条解释；必要限制与状态保留。对已完成只读查询不追加通用“查看账户与操作记录”；操作草案的尚未执行状态、确认以及失败重试按原合同保留。保持 UI 窗口刚完成的水平工具行、主题、阅读宽度和品牌样式，不恢复已经取消的完成后进度面板。

## 单一写入者

- 规划：仅新增 base_runtime/reply_presentation.py、本文件、outputs/reply-components-20261007。
- UI：现有 js/chat.js、js/bank_ui.js、chat.html、css/chat_reading.css，新增 js/reply_components.js、css/reply_components.css；本轮不触碰账户页或其品牌横幅。按既有 scope 补充精确注册。
- Agent构建：其现有 agent_runtime/conversation.py、bank_tools.py、data_boundary.py 范围，负责生产端接线、简短正文与最终审核。不要把展示补丁混进冻结 Jev 对照样本，进行中的 experiment-v8 先结束或保留其冻结副本。

## 投影接口 v1

result.reply_presentation = {
  schema_version: 'navren.reply-presentation.v1',
  status: 'verified',
  workspace_id, response_id, environment: 'simulation',
  blocks: [{id, type, title, period, currency, source, evidence_id, source_version,
    metrics: [{id, label, value_minor / value_scalar, currency, fact_id}],
    rows: [{id, label, date?, value_minor / value_scalar, currency, fact_id, account_id?}],
    note?, total_count?, has_more?}],
  text: 最终确定性事实文字或 null
}。

首批实际投影 type: fact_summary / transaction_list / category_bars / funding_breakdown / account_balances。period_series 留作后续具备真实序列数据后的扩展。

规划的 build_reply_presentation(workspace_id, response_id, facts, evidence, fact_ids, *, reviewed=False) 返回投影或 None。事实路径只用模型实际选中的 fact_ids；每个展示数值明确绑定被选中事实，不凭同额匹配。Agent owner 在 add_facts 本地受信事实附加 field=原字段名、path=原递归路径、label=已有中文标签、name=原受信对象名称，不能接受模型自由字段。日期、分类、币种与来源只从该事实同一 evidence 对象获取，不改变事实 ID。

审核路径不在审核后增加新的金额。初版可以使用本轮已经选择并核验的金额事实组成辅助组件，但必须把同一最终投影视图交给原审核和最终守卫。现有审核接口如不适合扩展，先只对 verified_facts 接通完整组件，reviewed_text 保留已审核正文和既有日历，不假称已全面覆盖。清楚报告真实范围。

statement_details 的 total_count 是完整筛选笔数，rows 只是当前页，has_more 不可当完整；0 笔与分页无记录分开。monthly_statement 的收入、消费、退款、转账、投资保持各自语义，净差额不等于余额。budget 的结余、底线、到期义务、预留与 investable_minor 只能来自同一账户、币种及版本；不足以完整解释扣减时列明已核验项，不绘制虚假的完备瀑布。annual_statement 不补未知年份、月份。

## UI 消费与交互

1. 本次真实回复进入组件渲染，允许无投影时正常保留原回复；类型未知、身份不符、isFallback 或投影不完整时不显示金融组件。
2. 事实投影使用 its text 替代同一事实的字段长列表；保留一个独立来源日期标签与必要限制。审核文字不得由前端重写、截断或去掉重要风险。
3. 数值右对齐、分类可展开、流水可点选在卡内展开完整已核验日期/类别/金额/来源；只读查询不借此自动准备操作。禁止从自由 reply、历史 HTML 或缓存猜金融数字。
4. 点击分类可发送带同一月份类别的受控明细问题；先支持普通问答，不产生金融授权。事件 handler 用 DOM textContent 和固定类型，禁止模型 HTML/JS。
5. 建议问题保存描述并恢复最新有效一组，字符串/对象走同一规范化入口，账户切换或失效取消。没有可用建议不凑数。
6. 刷新恢复已验证展示的原快照，明确历史范围；不能恢复交易可执行按钮。尽量保存响应结构，仅展示同一账户原结果；身份或版本无法确认时保留历史正文，提示重新查询。

## 本轮检查与采用

只做涉及文件语法检查与有限本地假响应/真实数据投影检查，不跑全套、不发付费模型请求。在独立预览页/独立标签页看紧凑流水、分类、资金三种效果及点击展开，不触碰用户当前聊天记录；不要建立收费实验。

由规划协调普通服务源码采用，不另起端口，不更换用户数据、不改账户、不影响已冻结实验。交付写明代码是否接通、当前展示范围与未覆盖组件；不以静态样稿或 HTTP 200 声称真实模型全流程通过。
