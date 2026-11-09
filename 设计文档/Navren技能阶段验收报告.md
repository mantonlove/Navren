# Navren 技能阶段验收报告

日期：2026-10-06。状态：本阶段采集和独立评分完成，语义未通过；缺陷转入 Agent 执行任务修复。

## 验收对象与判据

本轮验收实际开发源 `/Users/manton/CC/projects/Navren` 的金融技能、工具金额合同、真实 `/api/agent` 请求链与公共 RAG 消费。题集先冻结，再调用模型；10 道题、6 类合成夹具、52 条固定工具事实。主模型 DeepSeek，独立审核 GLM；模型审核的 approved 仍需逐句核对，不能直接计为语义 PASS。

必须全部 10 题分别通过，且没有实质金融错误、画像或金融写入，才能称这个小题集通过。被服务、预算或审核阻断的题不计通过；同一道题不同尝试不能拼接成一轮通过。单次小题集不能支持通用金融准确率、真实客户效果或生产可用性的结论。

题集 SHA256：`f92627f6dcd96a979d17d805dc5a219a47fcacc0a9e647c6f6e1a421f62ec0e6`。

## 已完成的证据

| 项目 | 当前结论 | 证据与限制 |
|---|---|---|
| 首轮真实模型金融语义 | 未通过：2 PASS、7 FAIL、1 BLOCKED | 按整段可见答复核对，包括额外段落；不是按关键词或审核批准率评分。 |
| 金融确定性合同修复 | 局部通过 | 修复后旧集成源的 80 项回归通过；该结果不自动覆盖随后 RAG 集成。 |
| 第二轮采集 | 未完成 | 只收集 5 题；快照遗漏概念 JSON，另有正常生成答复被 CSV 输入规则误判。 |
| 第三轮采集 | 未完成 | 只收集 2 题；旧 unavailable 夹具与新 public_catalog 接口不兼容，在模型调用前失败。 |
| 第三轮完整本地回归 | 未通过 | 原快照 102 项中 96 通过、1 失败、5 错误；修正测试夹具接口及私库存储读取后，定向 42 项中 38 通过。原失败断言保留。 |
| 第四轮本地完整请求链预检 | 链路通过 | 10 题、52 条事实一致；脚本主模型与审核模型，不证明金融语义。 |
| 第四轮完整本地回归 | 未通过 | 106 项全部执行，102 通过、3 失败、1 错误；保留 4 项数据边界集成失败。 |
| 第四轮真实模型采集 | 未通过：1 PASS、1 INCOMPLETE、8 BLOCKED | 10 题、52 条事实一致；2 条实质答复、8 条边界回退。原型工具事实与模型可见证据分开。 |
| 真实公共资料本地消费 | 最终快照下限定通过 | 扫描 102 条，导入 45、拒绝 57，发布 230 段；8 段原文定位吻合。5 段为目标 PMI 原文，2 段同月解读，1 段上月背景。 |
| 浏览器文本对话链 | 请求链 PASS、整段语义 FAIL | 独立临时合成库实际经 DeepSeek 3 次、GLM 1 次回执并显示预算答复；不合并进固定 10 题通过率。 |

首轮采集仅冻结部分源文件，10 个 workspace 共用一份临时数据库；这两点作为历史限制保留。修订后的运行器改为每题独立银行、个人、知识和市场库，并绑定生产代码、技能及实际运行数据依赖。第二、三轮的失败记录没有覆盖或删除。

## 最终逐题结论

| 题目 | 独立结果 | 原因 |
|---|---|---|
| S01 | INCOMPLETE | 金额及应急线判断正确；冻结必要点3只解释未来入账未确认，遗漏未录入支出与充分性不能完全判断。 |
| S02 | PASS | 9月6500元历史收入与未来60日已确认流入0准确区分，未把历史收入加入未来现金。 |
| S03 | BLOCKED | 正常摘要触发BOUNDARY_RAW_RECORDS，未进独立审核；可见fallback未回答缓冲充分性。 |
| S04 | BLOCKED | BOUNDARY_RAW_RECORDS导致只显示10276基线fallback，未向用户完成21天用途与研究判断。 |
| S05 | BLOCKED | BOUNDARY_RAW_RECORDS阻断，用户只见预算fallback，没有R4与40/60解释。 |
| S06 | BLOCKED | public_products正常公开产品查询被BOUNDARY_PUBLIC_QUERY拒绝，用户仅见37字通用fallback。 |
| S07 | BLOCKED | knowledge_search被BOUNDARY_PUBLIC_QUERY拒绝，没有公开政策或本人资格的可见实质答复。 |
| S08 | BLOCKED | BOUNDARY_RAW_RECORDS导致条件讨论未展示；状态核验通过不能代替答复完成。 |
| S09 | BLOCKED | BOUNDARY_RAW_RECORDS阻断已取证的导入/当前账户/全年覆盖解释，fallback未回答两项假设。 |
| S10 | BLOCKED | 同承诺去重工具合同正确，但BOUNDARY_RAW_RECORDS让用户看不到额外500与规划9776。 |

S01已正确区分预算与底线外余量，但遗漏未录入支出和应急充分性未知的必要说明。浏览器答复把9664元的订阅续费情景描述成全部周期候选，同时举出未纳入推演的房租1800元；审核批准未发现覆盖错配。

## 本任务已修复的金融边界

当前预算与未来底线外余量分开：主账户当前可安排 10,276 元，未来 60 天已确认事项后的最低余量 9,844 元；候选续费发生时为 9,664 元。后两项已经扣过应急底线，与零比较，不能再扣一次 10,000 元。正余量只证明已知事项下未破底线，不能判断应急缓冲是否充足。

规划留款必须先读取当前预算；同一信用卡义务由真实稳定 ID 去重。已有应还 2,000 元，把目标提高到 2,500 元只追加 500 元，情景可安排为 9,776 元。情景结果不能在审核失败时冒充当前预算。目标金额、正式风险等级、用户假设与持仓本金都新增了明确的口径说明；金融诊断、目标规划、资产配置、画像、知识检索及审核技能同步补充了这些规则。

## 已暴露的集成问题

正常公共查询“产品的赎回条件”及“复查补助”被公开词规范拒绝；合法公共原文中的 5,000 元解析事实，在出库投影与审核金额证据中被丢弃。原查询与 approved、金额单位、无交易断言均保留。另有生成答复包含多行中文逗号和日期时，被输入 CSV 判据误判为原始流水；同一判据还会滤掉部分 `load_skill` 的正文说明，仅保留目录信息。这些问题由独立 owner 完成明确交接后转入 Agent 窗口修复；本次验收没有覆盖其代码。

本任务侧的快照依赖遗漏、旧提供器接口和追踪兼容问题已修复。兼容夹具明确记录为空公开目录、ready=false 的不可用提供器；没有改题目、52 条 expected 或评分标准。最终候选为 `candidate-round-4-ready`，315 文件复制前后及副本一致；云运行绑定 183 项生产和运行输入，前后源码与题集一致。原仓库随后并行变更不自动属于本轮被测版本。

最终云结果 SHA256：`76cc3dec1109bf596c0768ff26de4a572b60396f1620b3ec04b88716f7d97bdb`。运行器 SHA256：`c3cf75416eb5306e0a2cf8e09b9222e68cf3deed4630cac6a7bad1de80ab215b`。全部金融题未确认执行金融操作，画像保持不变，新增观察与记忆均为零。`collection_complete` 和退出码 0 只表示采集完整且来源稳定。

## 可追溯产物

- [冻结题集](/Users/manton/CC/projects/Navren/evaluation/navren_skill_semantic_cases.json) 与 [评分标准](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/semantic-rubric.md)。
- [首轮逐题评分](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/semantic-grading-initial.md) 与 [全部尝试记录](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/attempt-ledger.json)。
- [确定性金融合同复核](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/financial-contract-review.md)。
- [新集成失败最小复现](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/cloud-round-3/regression-integration-findings.md)。
- [最终本地回归](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/local-final-round-4/local-final-regression.json) 与 [完整请求链预检](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/process-preflight-round-4/semantic-results.json)。
- [最终独立逐题评分](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/semantic-grading-round-4.md)。
- [最终云采集](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/cloud-round-4/semantic-results.json) 与 [候选快照清单](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/candidate-round-4-ready-snapshot.json)。
- [最终真实公共 RAG 独立核验](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/public-rag-independent-check-round-4.md)。
- [浏览器可见答复](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/cloud-round-4/browser-ui-ax.txt)、[实际模型回执](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/cloud-round-4/browser-session.json) 与 [预算截图](/Users/manton/CC/projects/Navren/outputs/skills-acceptance-20261005/cloud-round-4/browser-ui-result.jpg)。

本轮没有验证真实银行交易、语音识别、完整投资组合优化器、真实客户资格判断或公开部署。所有金融题使用隔离合成数据；语义标注由独立 AI 逐句核对，尚未经过人工金融专家签收。
