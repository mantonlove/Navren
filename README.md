# Navren

Navren 是面向银行与个人财务场景的 Multi-Agent 协同应用原型。主 Agent 理解问题、规划任务并调用业务工具，专门的检索、计算与审核组件分别提供资料和验证；账务计算、权限、用户确认与执行由确定性服务处理。

项目用于比赛展示与技术验证，目前仍在开发，尚未完成主流程整体验收。

## 在线体验

体验网址：[https://navren.cn/](https://navren.cn/)。评委可通过该网址访问 Navren，无需自行部署；体验功能可能需要注册或登录。账户与财务数据为模拟数据，资金操作仅在体验环境内生效。部分功能仍在开发或验证中，具体可用范围以实际体验为准。

## 功能范围

- 注册与身份：手机号注册、密码或短信验证码登录；注册账号与体验角色工作区分别绑定。
- 账户与业务：账户、流水、转账、AA 收款、银行卡、订阅及定时安排；AA 账单可关联订单与图片凭证。
- 对话与资料：账户查询、公开资料引用、操作草案及来源说明；聊天和图片、语音原件由服务端按账号和角色保存。
- 财务规划：财务日历、资金预留、现金流推演、账单分析与个人财务图谱。
- 产品与市场：公开产品和保险资料、带原文链接与发布日期的市场精选、周报及历史版本。
- 消息与个人中心：独立消息页面、个人信息、设置、收藏及逐条财务记忆管理。

页面、工具、运行前提和验证范围以当前代码及对应记录为准。历史报告、候选实现和测试命令不代表当前版本已经验收通过。

## 数据与执行边界

体验客户、账户和交易为人工构建的数据，用户通过体验角色使用功能，不发生真实资金划转。不同注册账号选择同一角色仍使用独立工作区。保存的数据需要服务端数据库与原加密密钥共同恢复，浏览器缓存不作为唯一存储。

用户导入的 CSV 流水独立用于分析，不自动改变体验账户余额或持仓。产品、政策和市场资料保留出处及原文日期；公开披露目录不等于实时可购买清单，模型审核也不能替代事实正确性。当前存在待修复和待验收项，不能据此将应用用于真实金融业务。

Navren 尚未连接真实银行、支付平台或证券资金接口。模型输出不构成执行授权；需要变更账务或账户状态时，仍须通过独立的用户核对、确认和对应验证流程。

## 快速开始（本地）

仓库不含运行数据库、媒体原件与任何密钥，干净检出**不能**直接启动；缺数据时启动脚本按设计拒绝启动，而不是退回匿名账户。完整步骤：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 0) 补齐体积较大的分词器资源（约 33 MB，按清单下载并校验 sha256）
.venv/bin/python scripts/fetch_dependency_resources.py --fetch

# 1) 一次看清缺什么（不写文件、不启动服务）
.venv/bin/python scripts/check_environment.py \
  --state-dir <已初始化的注册状态目录> \
  --financial-database <封存的四模板财务库> \
  --market-database <公开市场库>

# 2) 密钥只经环境变量注入，不写进 .env 或仓库
#    下面用 macOS 钥匙串读取作为示例，其他环境替换成等价的密钥注入方式
DEEPSEEK_API_KEY="$(security find-generic-password -s CC/DEEPSEEK_API_KEY -w)" \
GLM_API_KEY="$(security find-generic-password -s CC/GLM_API_KEY -w)" \
NAVREN_PERSONAL_DATA_KEY="$(security find-generic-password -s CC/NAVREN_PERSONAL_DATA_KEY -w)" \
.venv/bin/python scripts/serve_financial_frontend.py \
  --state-dir <同上> --financial-database <同上> --market-database <同上>
```

自检会列出状态目录、四个启动库、财务库模板、模型密钥、私库密钥校验、隔离 MCP SDK 和端口；每项失败都给出修复方向。全部通过后再启动，避免在启动日志里逐个试错。

## 部署准备

前端使用 HTML、CSS 和 JavaScript。Python 依赖见 `requirements.txt`；以下命令适用于已有仓库访问权限的 macOS 或 Linux 环境：

```sh
git clone https://github.com/mantonlove/Navren.git
cd Navren
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py --help
```

公开启动器使用注册入口。运行前需要在部署环境准备注册状态、四份体验财务模板、公开市场库和相应加密密钥；这些运行数据不随源码上传。已有数据库必须继续使用原密钥，缺少数据或凭据时不能回退到匿名账户绕过认证。

配置示例使用部署者自行准备的目录：

```sh
.venv/bin/python server.py \
  --state-dir ./runtime/state \
  --financial-database ./runtime/financial.sqlite3 \
  --market-database ./runtime/market.sqlite3 \
  --runtime-dir ./runtime/service
```

这些参数指向已初始化的数据，不会下载作者的数据库。启动后使用终端打印的地址；服务默认监听本机回环地址。服务器部署需要自行配置访问入口、HTTPS、WebSocket 反向代理及认证边界。

| 配置 | 用途 |
|---|---|
| `NAVREN_PORT` | 注册欢迎入口端口，也可显式指定 `--port` |
| `NAVREN_PERSONAL_DATA_KEY` | 个人资料和媒体原件加密密钥；恢复已有数据时保持一致 |
| `NAVREN_SMS_CONFIG` | 部署环境的短信通道配置文件路径 |
| `DEEPSEEK_API_KEY` | 主 Agent 的模型调用凭据（缺失时启动脚本直接拒绝启动） |
| `GLM_API_KEY` | 独立审核与市场编辑的模型调用凭据（同上） |
| `NAVREN_SMS_ACCESS_KEY_ID` 等 | 阿里云号码认证短信通道凭据；未配置时短信登录与高风险操作验证不可用 |
| `DASHSCOPE_API_KEY` | 启用百炼语音时的服务端凭据 |
| `DASHSCOPE_API_HOST` / `DASHSCOPE_WORKSPACE_ID` | 语音业务空间配置，按服务商实际设置填写 |

密钥由部署环境的密钥管理器提供，不写入代码、前端或命令示例。未配置模型、短信或语音服务时，相应功能无法完整运行。当前主对话链不解析聊天图片；图片原件保存与公开研究附件的提取分别由各自接口处理。

市场定时刷新由 `market_schedule.py` 与 `scripts/refresh_market.py` 管理；热点在北京时间每日 08:30、12:30、18:30 检查，失败按原重试上限处理，周报使用上一完整自然周。网络、主机可用性、来源与发布审核共同决定实际完成状态；配置定时任务不等于本次更新已成功。

## 页面

| 页面 | 内容 |
|---|---|
| `index.html` | 注册、登录与体验角色入口 |
| `chat.html` | 对话、引用资料与操作草案 |
| `account.html` | 账户、交易、AA 与其他体验业务 |
| `recommend.html` | 产品、保险和公开市场资料 |
| `messages.html` | 站内消息、提醒和关联事项 |
| `mine.html` | 个人资料、设置、记忆与收藏 |
| `copilot.html` | 顾问工作台 |

## 测试与验收状态

仓库包含账务、身份隔离、用户确认、幂等、资料绑定和前端交互等专项测试。**全量套件在干净检出下并非全绿**：2026-11-09 对 `main@5eca2da` 的实测为 3041 个测试中 299 failures + 591 errors，原因集中在缺少封存数据库夹具、未配置短信 MFA、模型密钥缺失与历史测试未跟上门禁变更。逐项原因与复现命令见 [测试现状](docs/test-status.md)。

因此 CI 只门禁已确认可在无状态环境通过的模块（`tests/ci_green.txt`、`tests/ci_green_js.txt`），全量结果作为不阻塞的报告单独发布：

```sh
.venv/bin/python scripts/run_ci_tests.py      # 门禁子集
npm ci && npm run test:ci                     # 前端门禁子集
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # 全量（当前会红）
```

本次代码同步不代表执行了全部测试或通过了真实模型、当前入口及全产品验收。旧版量表和评分不代表真实用户效果或比赛成绩。

技术结构见 [技术说明](TECHNICAL.md)。银行数据结构与第三方归属见 [银行说明](simulated_bank/README.md)、[第三方材料说明](simulated_bank/THIRD_PARTY_NOTICES.md) 和 [数据合同](simulated_bank/customer-contract.json)。运行数据库、媒体原件、凭据、缓存、下载的研究原件及本机验收记录不随源码发布。
