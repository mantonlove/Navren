# Navren

Navren 是一个财富顾问 Agent 原型：读取账户和账单，比较产品，解释风险与资金安排，并将可执行的建议交给用户确认。

本仓库包含前端、Agent、模拟银行、完整客户数据结构、演示样本和测试。当前银行功能使用自建模拟账户；没有接入真实银行或比赛官方接口。原项目名为轻舟（Qingzhou）。

## 运行

需要 Python 3.10 或以上版本。前端使用原生 HTML、CSS 和 JavaScript，无需构建。

```sh
git clone https://github.com/mantonlove/Navren.git
cd Navren
python3 -m pip install -r requirements.txt
NAVREN_PORT=18767 python3 server.py
```

打开 <http://127.0.0.1:18767>。每个浏览器会话拥有独立的模拟账户，首次访问自动生成演示数据。

不配置模型密钥即可演示账单分析、账户查询、模拟产品比较和准备操作。开放式对话、图片理解及云端语音服务需要服务端环境变量 `YUANJING_API_KEY`，不得把密钥放入浏览器配置或提交到 Git。语音还需要 `websocket-client`；图表使用 Chart.js CDN，语音识别取决于浏览器支持。

可选变量：

| 变量 | 用途 |
|---|---|
| `NAVREN_PORT` | 页面及同源接口端口，默认 8765 |
| `NAVREN_SIM_DB` | 模拟银行 SQLite 数据库的绝对路径 |
| `YUANJING_API_KEY` | 可选的模型及云端语音密钥，仅服务端读取 |

为保留已有演示账户，默认数据库仍使用 `~/Library/Application Support/Qingzhou/simulated-bank.sqlite3`。旧变量 `QINGZHOU_PORT`、`QINGZHOU_SIM_DB` 和浏览器中的 `qingzhou_*` 存储键继续兼容；改名不会清空余额、档案或阅读偏好。

## 页面和功能

| 页面 | 内容 |
|---|---|
| `index.html` | 欢迎页及阅读方式选择 |
| `chat.html` | 理财对话、账单分析、产品比较、操作准备 |
| `recommend.html` | 产品、配置建议与市场信息 |
| `account.html` | 现金、持仓、流水、待确认操作与订单 |
| `mine.html` | 阅读模式、字号、语音、档案与收藏 |
| `copilot.html` | 顾问工作台与客户交接信息 |

经典版、关怀版、青春版均使用白色界面，按字号、间距和信息密度区分。页面与资源共用内容版本，静态响应禁止缓存；旧网址、模式切换、前进后退和重新打开标签页会核对当前版本。

已接入模拟执行的动作是理财申购、赎回和关闭银行代扣。Agent 可以读取、比较和准备操作；用户必须在确认界面批准，理财操作另需模拟验证。关闭银行代扣不会取消商户订阅合同。转账、发卡、额度调整等业务尚未接入执行。

## 模拟银行及数据依据

模拟银行包含客户全档案、账户、流水、信用卡、贷款、保险、产品、适当性、授权及审计等结构。演示金额、交易和客户均为合成数据。

数据结构参考 Apache Fineract、IBM Synthetic Data Sets 和 Open Banking UK，固定来源版本、改编说明与许可证随仓库保留；这些来源不代表真实银行连接或机构认可。详见 [模拟银行说明](simulated_bank/README.md)、[第三方说明](simulated_bank/THIRD_PARTY_NOTICES.md)、[完整数据合同](simulated_bank/customer-contract.json) 和 [接口合同](simulated_bank/openapi.json)。

独立模拟银行服务可另行运行：

```sh
python3 -m simulated_bank serve --port 18766
```

主应用无需先启动独立服务；`server.py` 通过同源 `/api/finance` 使用银行核心。

## 验证

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

测试覆盖银行资金与记账、权限隔离、用户确认、并发及幂等、Agent 回归，以及旧网址和缓存恢复。`tests/v4` 至 `tests/v6` 保留研究评估脚本和量表；其中占位证据或历史评分不等于当前产品测试结果。

## 代码结构

```text
Navren/
├── *.html                     六个页面
├── css/                       页面、银行组件与共享设计样式
├── js/                        页面逻辑、导航、客户端及产品数据
├── server.py                  本地服务、版本更新、模型和语音代理
├── agent_core.py              顾问编排、检索与合规检查
├── finance_agent.py           确定性银行任务处理
├── finance_gateway.py         同源会话、授权与确认
├── simulated_bank/            银行核心、数据合同、样本及来源
├── tests/                     回归、HTTP 集成与评估脚本
├── scripts/                   数据同步及研究辅助脚本
└── TECHNICAL.md                技术说明
```

运行数据库、用户会话、日志、缓存和本地验收截图不提交到 Git。此仓库从整理后的源码建立全新历史，保留第三方材料的原有授权与归属。
