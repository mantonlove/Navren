# 本地与 origin/main 的分叉清单

> 统计时间：2026-11-09　本地 HEAD：`e5f4206`（2026-10-22）　远端：`origin/main@5eca2da`（2026-11-03）
> 生成方式：`python3 scripts/report_local_divergence.py --repo <本地仓库> --json`

## 结论

本地不是 GitHub 的超集，GitHub 也不是本地的超集：两边各有对方没有的内容。任何一侧整树覆盖另一侧都会丢代码。

| 指标 | 数值 |
|---|---|
| 提交差异 | 本地落后 58 个提交，领先 0 个 |
| 工作树未提交 | 186 个已跟踪文件修改、1 个删除、41 项未跟踪 |
| 内容不同的已跟踪文件 | 118 个 = 51 个「本地旧版」+ 67 个「本地独有」 |

- **本地旧版（51）**：文件内容与 `origin/main` 历史中某个版本逐字节相同，说明本地停在旧版本，远端后来改过。
- **本地独有（67）**：内容在 `origin/main` 全部历史中从未出现，是远端没有的本地工作。
- **未跟踪（41）**：Git 从未记录，包含源码、测试、样式，以及知识产权材料等非源码资产。

## 本地旧版（51）

这些文件**不能**直接覆盖远端，否则会把远端 58 个提交里的修改回退掉。差异最大的几处：

| 文件 | 本地行数 | origin/main 行数 | 判断 |
|---|---|---|---|
| `agent_runtime/conversation.py` | 3726 | 4674 | 远端新增约 950 行，本地缺 |
| `agent_runtime/tool_routing.py` | 155 | 278 | 远端扩展了路由逻辑 |
| `server.py` | 915 | 964 | 远端新增入口 |
| `js/chat.js` | 2763 | 2873 | 远端新增前端逻辑 |
| `base_runtime/service_assembly.py` | 182 | 184 | 小改 |
| `README.md` / `TECHNICAL.md` | 159 / 140 | 91 / 69 | 本地版本更长，远端是精简后的版本，需人工合并 |

其余按目录分布：tests 7、local_knowledge 6、skills 6、evaluation 5、agent_runtime 5（合计含上表）、base_runtime 3、js 2、scripts 2、simulated_bank 2，以及根目录 `market_*.py` 等 10 个。

## 本地独有（67）

这些是真正需要并入的本地增量，按主题可分四批：

| 主题 | 目录 | 数量 | 说明 |
|---|---|---|---|
| 对话界面组件 | `js/` 25、`css/` 3 | 28 | 附件、历史视图、面板、组件系统等前端实现 |
| 运行时契约 | `agent_runtime/` 12、根级 6 个 py（`server.py`、`welcome_auth.py`、`sms_auth.py`、`registered_instances.py`、`finance_gateway.py`、`wealth_portfolio.py`） | 18 | 服务端注册、短信、财务网关与资产组合 |
| 前端页面 | 根级 6 个 html（`index`/`chat`/`account`/`recommend`/`messages`/`mine`）+ `README.md` | 7 | 页面结构与说明文档 |
| 评估与模拟 | `evaluation/` 4、`simulated_bank/` 2、`local_knowledge/` 1、`scripts/` 2 | 9 | 评估夹具、手动订阅、部署校验脚本 |
| 测试 | `tests/` 5 | 5 | 与上述前端/契约配套的用例 |

## 未跟踪（41）

| 类别 | 条目 | 处理建议 |
|---|---|---|
| 源码与样式 | `css/` 9、`js/` 5、`agent_runtime/` 2、`base_runtime/` 1、根级 5、`local_knowledge/` 1、`simulated_bank/` 1、`scripts/` 1、`skills/` 1 | 按主题并入，先跑对应模块 |
| 测试 | `tests/` 11 | 与源码同批并入 |
| 资源清单 | `dependency-resources/deepseek-v41-1017/` | 已由 `scripts/fetch_dependency_resources.py` 清单覆盖，不再入库 |
| 非源码资产 | `知识产权-20261109/`、`知识产权-20261109-交付包.zip`、`设计文档/软著准备-20261030/`、`设计文档/轻航完整行动计划.md` | **不并入公开仓库**；涉及权属材料与内部计划，按仓库边界留在本地 |

## 建议的合并顺序

1. 以 `origin/main` 为基线（本次即在工作树 `_related/navren-hardening-20261109` 上做）。
2. 先并入「运行时契约」批（agent_runtime 12 + 根级 13），跑 `scripts/run_ci_tests.py` 与受影响的 HTTP/注册模块；这批决定服务能否起来。
3. 再并入「对话界面组件」批（js 28 + css），前端无构建，逐页手测加 `node --test` 对应文件。
4. 然后并入评估与测试两批，补齐 `tests/ci_green.txt`。
5. 51 个「本地旧版」逐个人工 diff：只有确认本地版本含远端没有的修复时才保留，其余一律取远端。**不要**用本地文件批量覆盖。
6. 非源码资产不入库；如确需版本化，另建私有仓库或加密归档。

## 风险

- 本地 51 个文件覆盖远端 = 回退 58 个提交，尤其 `conversation.py` 差 950 行。
- 前后端两批互相依赖（组件契约、事件名），分开验证会得到假绿；每批合完都要跑一次门禁子集。
- 共享工作树有多个会话并发写入（`git status` 里 186 个改动并非都来自同一批工作），合并前先确认每个文件的负责人。
