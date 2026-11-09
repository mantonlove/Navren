# Navren 技能与 RAG 消费合同双模型复查

复查日期：2026-10-05。产出者：Codex。正式源码：`/Users/manton/CC/projects/Navren`。

**最终门禁：PASS。初审两位均 REVISE；修复及本地验证后，两位复审均 PASS，审查覆盖 complete，置信度分别 0.86 与 0.85，无阻断项。** 两轮均真实调用，均走 Aiping 主路由，没有使用备用渠道、续审或以本地自检代替正式双审。

本轮复查采用 [deepseek-review 技能](/Users/manton/.codex/skills/deepseek-review/SKILL.md)。

## 两位模型的独立意见

| 轮次 | 实际返回模型 | 实际渠道 | 门禁 | 置信度 | 请求次数 |
|---|---|---|---|---|---|
| 1 | DeepSeek-V4.1-Flash | Aiping / primary | REVISE | 0.85 | 1 |
| 1 | GLM-5.3-Flash | Aiping / primary | REVISE | 0.86 | 1 |
| 2 | DeepSeek-V4.1-Flash | Aiping / primary | PASS | 0.86 | 1 |
| 2 | GLM-5.3-Flash | Aiping / primary | PASS | 0.85 | 1 |

DeepSeek 初审指出：同 `(citation_id, as_of)` 的再次检索覆盖已提供快照，使首次公共金额脱离最终失效检查。复审认为登记前完整投影比较、保留首快照及三层回归关闭了该旁路，技能注册、角色隔离、权限和阶段声明自洽。

GLM 初审指出同一阻断项，并要求修复及明确验收记录。复审认为代码、BankTools、主循环的阻断证据与 75 项测试相符，真实 RAG、优化器和金融语义质量的未完成披露准确，可作为本阶段交付。

两者没有阻断意见分歧；共同阻断项已采纳，两轮均无单模型意见抵消另一模型意见的情况。初审包与修复后包分别冻结，两个模型每轮收到同一脱敏包与同一 review_id/artifact_sha256。

## 实际修复与复现

原问题可在合成夹具中真实触发：第一查询原文为 5000 元；第二次 `search_public` 内修订为 10000 元，旧快照被覆盖，而 BankTools 仍保存首次金额事实。程序新鲜度检查通过，脚本审核器批准后主循环能展示旧金额。脚本审核器结果不证明真实 GLM 必然会犯同样的错。

修复在 `knowledge_retrieval.py` 登记新命中前比较同键完整安全投影；不同即抛 `stale_knowledge_evidence`，使用 `setdefault` 保留首次快照。BankTools 将该失效转为明确的 `STALE_KNOWLEDGE_EVIDENCE`，不登记第二份证据或新金额。保留全部旧引用供最终回查，不扩展技能和工具执行权限。

修复后实际输出：旧快照仍在；直接消费端和 BankTools 均拒绝修订；主循环 blocked；旧 5,000.00 元未展示；脚本审核器调用 0 次；bank_actions 为空、草案数 0、余额未变化。主模型即使尝试沿用旧答复也被程序门拦截。正常同投影重复查询继续可用，不同日期分别保留，单次多个引用回查中发生修订也被 BankTools 返回前检查拦截。

## 本地验证

- 75 项合并测试通过，失败 0、错误 0：技能注册/加载 10、公共知识消费 23、技能主循环及 BankTools 11、原原生对话回归 31。
- 新增 7 项回归覆盖上述修订时机及正常重复、不同日期、多引用回查等边界；独立子代理另用原触发器重做 5 组探针，结果符合预期。
- 17 份技能格式通过，16 份主目录技能，39 个现役白名单工具。8 份上游本地文件与固定提交清单的 SHA256 一致。此项为本地快照完整性核验，本轮未重新访问 GitHub 核验原始归属。
- 测试期间登记的候选源码和关键依赖 SHA256 无变化，二审完成后再次比对候选快照也无漂移。
- `conversation.py` 在初轮本地证据之后由另一窗口更新；本轮未写该文件。修复后测试与二审使用其当时版本 `c6fedacbc7f2a5f893294daa675f799868aced08111512ab735d307f8eca1b32`，不把前后依赖当成同一快照。

## 非阻断建议与保留限制

两位模型共同建议补一次独立人工标注题集或云复验，验证最后技能文字中的定性结论约束。该建议保留为下一阶段事项：本轮代码双审与合成协议测试没有完成真实模型金融语义验收，`cloud_model_semantic_verified=false` 保留。上轮云模型曾出现应急金重复扣除、零确认流入含义错误、无依据称缓冲薄等问题，不能因独立 GLM approved 隐去这些事实。

DeepSeek 另建议明确字符定位逐字核验的责任。当前模块已声明提供方必须核对原文内容，消费端只检查投影及定位结构/长度；此责任不被“长度相同”替代。本轮未宣称真实原件逐字定位已验收。

GLM 另建议强调直接消费类调用者展示前必须执行 `assert_fresh`：空命中后旧 ready pin 的观察已登记，应用 BankTools 返回前检查能阻断失效提供器且不登记新证据。直接类调用者必须遵守展示前回查约定，该状态观察本轮未修补，不列入已修复清单。

两位模型只审核收到的文本，没有执行本地测试、读取真实客户原件、访问外部仓库或检查页面/语音/视觉效果。传输脱敏遮了 URL 校验与部分初始化/错误消息行：初审 13 行，复审 33 行（含前轮审核元数据）；不能据这些占位符推断本地代码缺失，也不声称模型核验了被遮行。

本阶段通过范围：应用 skills、动态加载、业务绑定、公共 RAG 消费合同及对应声明。真实提供器、真实原件检索、收益优化器和完整金融方案质量仍待验收；没有重启共享常驻服务，未证明用户页面已加载本轮 runtime，未完成真实交易接入。

## 原始记录

- [初审完整双模型结果](/Users/manton/CC/projects/Navren/outputs/skills-review-20261005/round-1/review.json)
- [初审源码与旧漏洞证据](/Users/manton/CC/projects/Navren/outputs/skills-review-20261005/round-1/local-code-review.md)
- [逐项处置与初审原意见](/Users/manton/CC/projects/Navren/outputs/skills-review-20261005/round-2/prior-review-and-disposition.json)
- [二审完整双模型结果](/Users/manton/CC/projects/Navren/outputs/skills-review-20261005/round-2/review.json)
- [75项完整测试输出](/Users/manton/CC/projects/Navren/outputs/skills-review-20261005/round-2/test-output.txt)
- [测试、格式及源hash清单](/Users/manton/CC/projects/Navren/outputs/skills-review-20261005/round-2/test-summary.json)
- [修复后实际复现结果](/Users/manton/CC/projects/Navren/outputs/skills-review-20261005/round-2/fix-proof.json)
- [独立本地补充探针](/Users/manton/CC/projects/Navren/outputs/skills-review-20261005/extra-local-checks.md)
- [当前实施验收记录](/Users/manton/CC/projects/Navren/outputs/skills-acceptance/validation.json)

初审 review_id：`9ec4aed5-237b-40c9-8558-033007c026cf`。复审 review_id：`7b049e57-4abb-4d42-af27-98df484ca3c4`；复审包 SHA256：`afb3686454b1d5eeba7dcdb8eb3d925864fd63a9dec7e5a2625a674ebdf955f4`。
