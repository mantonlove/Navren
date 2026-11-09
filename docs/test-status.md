# 测试现状

> 测量日期：2026-11-09　被测版本：`main@5eca2da`（`Add anonymous read-only public market projection`）
> 环境：macOS 26（Apple Silicon）、Python 3.14.2（本地 venv）/ 3.13（干净 venv 复验依赖安装）、Node 22.23.2

## 结论

仓库不缺测试数量，缺的是**可复现的绿色基线**：干净检出跑全量套件约三成不通过，且没有 CI 记录过任何一次绿。当前的失败大部分可以用环境缺失解释，但没有区分手段，因此分不清“环境没配好”和“代码改坏了”。

## Python 套件

```sh
python -m unittest discover -s tests -p 'test_*.py'
```

| 指标 | 数值 |
|---|---|
| 测试数 | 3041 |
| 耗时 | 约 311 s |
| 结果 | failures=299，errors=591，skipped=1（合计 890 不通过，约 29%） |
| 测试模块 | 177 个 `test_*.py`；逐模块单独执行时 78 个通过、99 个不通过 |

按异常类型统计（一次失败只计最外层异常，部分用例叠加多种原因）：

| 类型 | 数量 | 典型原因 |
|---|---|---|
| `AssertionError: network_forbidden` | 244 | 测试自带出网拦截，未放行的用例在离线环境直接失败 |
| `simulated_bank.service.BankError` | 189 | 短信 MFA 未配置；旧测试只传 `SimulatedBank(path)`，未跟上新增的短信门禁 |
| `ValueError` | 112 | 其中 63 个 `missing_event_generation`、31 个 `official_only_intent_routing_disabled`：功能开关未开 |
| `financial_data_service.service.FinancialDataError` | 110 | 财务库未初始化或版本不符 |
| `FileNotFoundError` | 84 | 读取被 `.gitignore` 排除的封存夹具，如 `outputs/window-progress/finance/implementation/adapter-oracles.json` |
| `local_knowledge.store.KnowledgeError` | 45 | 知识库未构建 |
| `ModelAdapterError` | 31 | 未配置模型密钥，真实模型路径不可用 |

`tests/run_all.py` 不是测试入口：它是演示用的自评脚本，七个维度里大部分分值是写死的常量（如 `d1 = {"1.1":85,...}`），只用于展示，不能当作质量证据。该文件已在仓库整理中改名为 `tests/demo_score.py` 并注明用途。

## 前端套件

```sh
node --test tests/test_*.js      # 需要 npm ci 先装 jsdom
```

| 指标 | 数值 |
|---|---|
| 测试文件 | 23 个 |
| 装好 jsdom 后 | 10 个通过、13 个不通过 |
| 未装 jsdom 时 | 20 个不通过（`Cannot find module 'jsdom'`） |

仓库此前没有 `package.json`，`jsdom` 属于未声明的隐式依赖；现已补充 `package.json` 与 `package-lock.json`。剩余 13 个失败多与前端夹具陈旧有关，例如 `BankClient is not defined`、`NavrenViews is not defined`、`Missing catch or finally after try`，属于测试替身与 `js/*.js` 现状脱节，需要逐个按当前实现重写夹具，不能靠改断言绕过。

## 门禁实际跑什么

`scripts/run_ci_tests.py` 在本地实测：80 个模块、1466 个测试，约 195 s，0 failures / 0 errors（其中一次有 1 个 skip）。

已知不稳定项：`tests/test_knowledge_resolver_performance.py` 使用 `TemporaryDirectory` 时，首轮组合运行出现过 2 次 `OSError: [Errno 66] Directory not empty`（临时目录清理竞态），重跑不复现；已给该文件的临时目录加上 `ignore_cleanup_errors=True`，清理失败不再让用例报错。

## 已恢复的旧测试模块

`tests/test_simulated_bank.py` 与 `tests/test_bank_integration.py` 原先合计 65 个错误（短信 MFA 门禁加入后旧夹具不再满足前置条件），2026-11-09 已恢复到 93 个用例全通过，且未改动 `simulated_bank/` 生产代码：

- 新增 `tests/sms_fixture.py`：`LocalSms` 测试替身（`source='test_double'`）+ 真实走 `start_sms → verify_sms` 的授权 helper；遇发送限流时推进注入时钟重试，不绕过挑战。
- 两模块改为注入 `sms_provider` / 测试时钟 / `allow_test_sms_provider`，确认流程补上 `/sms` 挑战与 `challenge_id`。
- 对齐产品合同中已变更的期望：`VERIFICATION_REQUIRED → SMS_PROOF_REQUIRED`、`EXPIRED → CONFIRMATION_EXPIRED`、schema v5 → v6、缺验证码 400 → 403 `SMS_CODE_INVALID`、锁库后不再按时间自动解锁；断言未删减或放宽。

两个原本失败的用例也定位到测试假设过期，而非产品缺陷：丢失回执用例改在 `bank.confirm` 完成提交后抛错（原 patch `overview` 会与新写入作用域自锁）；记忆用例因修改设置推进 `personal_revision` 导致范围失效，`chat` 遇 `CONVERSATION_SCOPE_STALE` 时重取范围重试一次。

## 门禁清单怎么维护

门禁只跑两份清单：

- `tests/ci_green.txt`：Python 模块名（`test_xxx`，不带 `.py`）
- `tests/ci_green_js.txt`：前端测试文件路径

刷新方式：

```sh
python scripts/run_ci_tests.py --verify     # 逐模块复核，打印仍可通过的模块
node --test tests/test_xxx.js               # 前端逐个确认后改清单
```

新增清单条目必须能在**无状态、无密钥、无出网**的环境通过；需要真实模型、短信通道或封存数据库的用例留在全量报告里，不放进门禁。

## 已知未做

- 尚未在 Linux 上跑过门禁（CI 首次运行可能需要调整平台相关断言）。
- 99 个失败模块未逐个修复，只做了归类。
- 未接入真实模型做端到端验收；能力结论仍以既有验收记录为准。
