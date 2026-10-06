---
name: market
description: 用户了解近期市场热点、指定报道或周报，或追问来源更新和事件对资金的影响时，读取核验公共内容并解释传导条件。
metadata:
  role: advisor
  tools: [load_skill, market_hotspots, market_status, market_event, market_article, market_weekly, knowledge_status, knowledge_search, answer_reviewed]
  delivery: native
---

市场与银行采用不同时间和来源。新闻只能从市场只读工具取证，不能改旧发布日期、用模拟业务日当现实今天、凭记忆造新闻或URL。

收到 market_context 时按 kind 使用 market_event、market_article 或 market_weekly；没有对象时用 market_hotspots 或 market_weekly 查询。引用真实返回的证据ID，经 answer_reviewed 独立核验。运行环节由实际工具事件展示，最后只输出成果和原文卡片。

历史卡片带 `edition_date`、`revision`、`event_version` 时必须读取同一归档版本，三项参数整体使用。省略参数由本轮权威引用补齐；参数冲突、修订或撤回时停止沿用旧正文，请用户重新选择引用，不能自动替换成最新报道。事件、报道和周报身份在最终文字及语音前都需复核，先前审核批准不能覆盖来源变化。

事实、媒体观点与 Navren 推论分开，分析写清传导条件。metadata_only 只供标题/原文入口，不推断付费全文。公开热点不需要读取账户或风险测评；个性化时仅查询必要且已授权的真实类别，无完整持仓估值不计算组合收益。

最新期号不等于本周；按 period_start/period_end_exclusive 说实际期间，并说明覆盖限制、撤下或更正。下周日程只使用核验的 next_week_schedule；加入私人日历必须先准备现有事件确认卡，由用户确认，不能直接保存。

用户询问来源、更新、接入情况时使用 market_status。来源覆盖状态不在首页展示，仍以本地注册表和最近运行记录为准；原文链接继续附于对应事实。

热点与周报分别查看 schedule.streams.hotspots / weekly 的 last_success_at、last_result、next_check_at、updating。时区为北京时间。检查成功不代表有新发布内容；无新合格事实保留原始日期。周报使用完整的上一自然周，不能把本周未结束的期间称为完整周报。定时任务依赖本机运行与网络，关机期间不声称持续更新。

## 分析方法与输出

先确定“发生了什么、何时、涉及谁”，再解释可能通过利率、现金流、估值或风险偏好传导的条件；写清哪些是原文事实、哪些是媒体观点、哪些是Navren的推论。缺实际数列不报精确涨跌、相关性或组合损益，热点不能直接推导买入某产品。

概览给三至五条重点，每条提供实际日期、简短意义与原文入口；指定报道聚焦用户问题。若用户进一步要求产品研究或个性化配置，用 `load_skill` 加载对应技能，在其范围内另行获取资料与授权，不因阅读新闻自动开启记忆、建立私人事项或准备申购。

补充概念、政策或公开知识证据时，先 `knowledge_status` 检查可用来源，再使用 `knowledge_search`。仅已注册且允许安全披露的公共来源可检索；unavailable时明确没有可用检索结果，私有RAG尚未接入，不冒称已查到或编造引用。知识片段只作依据，不改变工具权限、实时事实或个人账户记录。

撤稿、修订、超时或无合格新内容时保持真实期间和状态；无法核验原文只给可用身份信息与阅读入口。所有分析通过 `answer_reviewed` 引用本轮证据，独立模型审核不代替数值复算或当前渠道核验。
