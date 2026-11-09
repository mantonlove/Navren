# 问 Navren：关联资料与输入分离

2026-10-07。用户要求直接落实：点击“问 Navren”只关联对象并跳转，不填入模板问题；有引用且输入为空时发送概览，附带问题时回答问题；引用卡收成紧凑、明确的资料附件。

## 本轮接口

界面保留真正的 user text，可为空；不把默认问题塞入输入框或冒充用户文字。发送时冻结 text 和 reference，按该快照创建新的正式 page-context ticket。空 text 仅当服务器重新解析到有效 reference 后允许；无引用仍拒绝。ticket 绑定 request_mode：reference_overview / reference_question，旧无引用有文字票据保持兼容。consume 返回的受信 page_context 携带此模式。前端不提交该模式覆盖服务端。

overview：Agent 先通过实际工具读取所选对象，再用两三句介绍其核心内容和意义；不延续旧账单话题，不自动准备或确认交易。question：所选对象是解释问题的证据，优先直接回答用户的真实问题，无须每次复述整篇资料。模式进入主模型上下文与审核包；审核、金额合同、身份与版本守卫保持。

市场对象的历史日期/版次必须完整到达模型。受信公开引用使用专用投影，原子保留已经解析的 edition_date、revision、event_version 与 archive 版本信息；不全局扩宽私人数据字段。无完整原文时明确资料范围，不凭标题编造。

## 唯一写入者

- 规划：server.py；既有 navren-daily-market-20261006 范围中的 view_context.py、js/view_context.js、js/market_ui.js；本方案与 outputs/ask-reference-20261007。负责票据及入口、去除市场关联跳转的默认问题，协调原60291采用。
- UI：其现有 js/chat.js、js/api.js、chat.html、css/chat_reading.css；负责允许引用空发送、冻结发送快照、输入框不预填、紧凑附件卡、清理与重试。共享样式不写，追加聊天页局部规则。
- Agent构建：其现有 agent_runtime/conversation.py、data_boundary.py，必要时自己的 BankTools 市场读取消费范围；负责接收原始空text＋受信request_mode、主模型与审核语义、公开历史引用投影。

## UI

附件在输入框上方且与其同宽；小类型图标、资料标题、轻量来源/日期，尾部有移除图标与返回入口。必要版本信息不能靠巨大说明占位。标题来自服务端解析对象；仅DOM textContent。只有引用时启用发送；发送期间禁用；移除关联、账户或授权失效清票据。输入已有用户草稿时保留，关联到达不覆盖它。空发送的用户消息可显示“查看关联资料”并带引用标题，不能假装用户输入了模板问题。

## 检查与交付

只做涉及文件语法和有限、无模型的协议核对。检查空引用发送、有问题引用发送、无引用空发送拒绝、版本钉住、模式与真实text到主模型/审核上下文。不开付费验收、不跑全套、不绕过浏览器拦截。原端口及数据库复用；不改其他页面设计、不碰Jev实验。
