# Navren 独立财务数据库与 MCP 实施方案

> 历史设计或实施记录：保留文内注明版本的测试结论，不能作为当前版本的验收结果。当前实现与运行方式见 [README](../README.md) 和 [技术说明](../TECHNICAL.md)。本机机器信息已清理，未发布的历史产物不提供仓库链接。

用户已于 2026-10-07 批准更新四个演示账户的底层数据库，并采用独立库、MCP 接入。实施源码为 `项目根目录`。本方案替代“每人只有三个通用现金账户、银行卡关系缺失”的初始化方式。

## 本轮交付

建立一个独立 SQLite 财务库，覆盖四个用户 2016-10-04 至 2026-10-03 的历史；提供明确的账户、银行卡、资产、合同、交易、保险与服务关系。产品继续使用现有登录身份，通过稳定映射读取新库。Agent 通过真正的 MCP 客户端调用受限数据工具；浏览器通过原有同源后端读取同一事实源。

SQLite 是本机第一版的持久层，服务接口与存储实现分开。服务器迁移采用相同服务接口和环境配置，不把 macOS 路径写死在客户端。前端不直接接触 SQLite 或 MCP 凭据。不能以新建库或 tools/list 成功冒充实际 Agent 消费已经完成。

## 业务结构

- 用户 → 机构连接 → 金融账户；银行卡作为账户凭证，独立账户分别记账，共用账户的卡不复制资产。
- 账户类型包括银行结算账户、信用卡账户、证券资金账户、支付钱包；支持多机构、多卡，不硬编码每人张数。
- 存款合同、投资持仓、贷款、保险合同、代扣授权分别登记，关联所属用户与资金账户。电子账户与非现金资产允许没有实体卡。
- 实物资产包括房屋、车辆、农机及个人股权；资产与租赁、保险、抵押关系明确。公司法人资产不进入个人资产。
- 十年交易必须有账户、发生日期、入账日期、状态、金额、币种、对手与业务类别；适用时有卡、订单、合同、原交易及配对转账 ID。
- 同一账户换卡保留历史，多卡共享授信不累加额度。银行卡挂失不自动视为银行账户冻结。

保险作为独立合同类别：投保人、被保人、受益人（适用时）、保障对象、承保机构、产品/条款版本、保障期间、保额、保费、免赔、缴费账户、理赔与状态。保额不加入资产；具有现金价值的合同单独记录价值及可取用条件。

四用户差异：首富为个人物业/股权/投资及家财保障；种粮大户为季节性粮款、农资、设备、种植险及适用补贴；包租婆为工资、三处租约、物业保障、按揭；创业阿姨为工资转养老金、餐饮经营结算、相应财产和责任保障。车辆与车险按实际合成资产关联，不要求人人拥有同一配置。

同标的、同期间的农业险不能随意重复投保；农户自缴与财政补贴分开。条款按年份、地区、用途生效，不能用今天政策填满过去十年。无法获得官方历史条款的部分明确为合成规则或未知，不伪装成真实保单。

## 事实与来源

个人财务事件、身份、保单号、卡号均为合成数据。真实业务结构参照公开银行业务说明和保险政策；每条规则保留来源链接、生效期间、核验日期。实际产品条款与合成配置分别标记；无可验证历史净值不得生成并标为真实收益。

已查参考：
- 招商银行账户/子账户：https://www.cmbchina.com/cmbpb/pfhelpurl/v60/HelpPage/203/03011.htm
- 招商银行用卡与投资业务：https://www.cmbchina.com/ykzn/index.html
- 建行信用卡主附卡账户关系：https://ebank.ccb.com/cn/creditcard/service/card_zhangcheng.shtml
- 三大粮食作物农业保险政策：https://jcs.moa.gov.cn/zcjd/202407/t20240702_6458234.htm
- 保险合同内容：https://www.nfra.gov.cn/cn/view/pages/rulesDetail.html?docId=879931
- MCP 官方 Python SDK：https://github.com/modelcontextprotocol/python-sdk

## 提供端合同 v1

`financial_data_service.service.FinancialDataService(database_path)` 提供 `query(subject_id, operation, arguments)`。subject_id 由可信会话映射或 stdio 进程启动配置指定，不作为模型可填的工具参数。数据库实体统一通过 subject_id 校验归属。禁止任意 SQL、任意文件路径、跨账户主体读取。

支持操作及参数：

| operation | arguments |
|---|---|
| list_connections | {} |
| list_accounts | {} |
| list_cards | account_id? |
| get_account_overview | account_id |
| list_transactions | account_id?, start_date?, end_date?, limit=50（最多200）, offset=0 |
| list_positions | account_id? |
| list_insurance | status? |
| get_policy | policy_id |
| list_assets | {} |
| list_obligations | start_date?, end_date? |
| get_financial_summary | as_of? |

统一响应：`schema_version="navren.financial.v1"`、`subject_id`、`source="navren_synthetic"`、`dataset_version`、`as_of`、`data`、`evidence`；金额为整数人民币分，未知值为 null，不填成零。列表有限分页；摘要不返回完整十年明细。错误响应带稳定 code，不吞异常伪装空数据。

第一版 MCP 只开放上述读取工具，以官方 SDK 的 stdio 传输实现；启动环境 `NAVREN_FINANCIAL_DB` 与 `NAVREN_FINANCIAL_SUBJECT_ID`，工具名为 `financial_` 加 operation。Agent 按登录工作区维护隔离客户端，显式映射主体，数据继续经过本机出站裁剪。日志只写 stderr，stdout 保留协议。

转账、申赎、改代扣等继续走已有草稿、确认、幂等和权限流程。MCP 第一版不开放写库、确认凭据或交易执行工具。以后增加写工具时仍调用服务端业务接口，不能用 SQL 替代业务校验。

## 单 owner 与分工

- 规划：本方案、`financial_mcp/`、`outputs/financial-mcp/`；实现 MCP 提供端和有限协议检查。
- 基座：`financial_data_service/`、新库生成/迁移、必要的既有后端兼容接线；负责四账户数据与事实源一致性。根配置或已有文件先登记范围、保留当前其他修改。
- Agent构建：`agent_runtime/financial_mcp_client.py` 及其现有消费模块；完成现行 AiPing 配置工作后接线，不改变模型、密钥或审核策略。

不得复用已经失败的全站验收任务。只做此次生成完整性、金额与归属、四用户读工具、跨主体拒绝及 MCP initialize/list/call 的必要检查，不运行付费模型，不开展全站浏览器或完整回归。

## 迁移与回退

旧十年源库保持只读保留，输出新版本独立库，生成至临时文件后完整落盘并原子发布。关联迁移不能仅随机分摊原交易；按收入来源、经营用途、合同、资金转移构造账户级可回放历史，并明确合成来源。

现有登录身份、记忆、对话和已执行操作记录保留。运行层账户映射与历史库资产汇总一致；在明确切换到新版本之前不重启或替换现役服务。交付明确记录“已生成 / 已接线 / 已启用”，不混为一个状态。备份旧库和切换配置，能够恢复。

## 本轮必要结果

四人都有独立、多机构账户和卡片关系，十年覆盖完整；资金、投资、负债、保单及服务查询能找到关联实体；任何同主体多卡不重复统计，跨主体 ID 被拒绝；借贷额度和保额不计入现金资产，信用卡还款不再计一次消费。MCP 返回当前版本有界数据，Agent 实际工具执行能够走 MCP 且通过既有出站裁剪。服务器暂未部署，保留迁移配置与说明。
