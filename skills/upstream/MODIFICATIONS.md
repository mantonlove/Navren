# 开源方法的来源与修改说明

本目录原文快照来自 Anthropic 公共仓库，版权与 Apache-2.0 许可证保留在各仓库子目录的 LICENSE。快照不进入 Navren 主 Agent 技能注册，也不会被自动当作应用指令加载。

固定提交及逐文件 SHA256、原始链接见 [provenance.json](provenance.json)。新增 Navren 文件为改写后的衍生方法；原文快照保持原样。

| 上游方法 | Navren 衍生技能 | 主要修改 |
|---|---|---|
| knowledge-work-plugins / search-strategy、knowledge-synthesis | knowledge-retrieval、product-research | 中文任务与产品主体/份额/渠道/效期核验；只接受本地白名单公共证据；删除外部企业搜索连接器、任意文件和 SQL 权限 |
| knowledge-work-plugins / validate-data | product-research、reviewer | 金融口径、明确缺项、确定性金额来源、独立审核 JSON、用户确认与模拟/真实来源边界 |
| claude-for-financial-advisors / prospect-intake | financial-diagnosis | 中国个人财务口径、导入流水与余额分开、正式风险画像与本人陈述分开；删除 CRM/邮件和美国账户制度 |
| claude-for-financial-advisors / portfolio-rebalance-review | portfolio-review | Navren 独立历史权益核算、中国渠道/费用/退出、已确认目标；删除美国税务、外部账户连接器及默认再平衡阈值 |

适配只复用工作方法。账户权限、业务规则、计算、操作草案及确认继续由 Navren 现有后端实现。goal-planning、asset-allocation 以及其他业务技能为本项目维护的原生方法；尚未接入的检索或求解组件不会因技能文档存在而变为可用。
