# Jev 接入与对照试验

> 历史对照试验说明：下列结果和限制属于文内日期及版本，原始运行报告未随仓库发布。运行命令需先在环境中配置相应密钥。

实现位于 `agent_runtime/jev_client.py`、`agent_runtime/jev_agent.py`；独立服务入口为 `scripts/serve_jev_agent.py`，默认 `off`。`active` 仅预载达到冻结阈值的一个维护技能，`shadow` 只计量和记录选择。

截至 2026-10-07，Navren 的模型请求统一要求经 AiPing Key。Jev 的 `jev-1.13.0` 未出现在本次查询的 AiPing 模型目录中，因此线上 `shadow` / `active` 和 Jev 付费对照采集会拒绝运行；保留本地注入 judge 的免费协议测试。不能把 AiPing LLM 替代判别器冒称为 Jev。

首轮正式结果曾记录在本地报告中，原报告未随仓库发布。40 次真实调用已完成，未满足默认启用条件；尚无有效答复条件下的准确率、速度或费用提升证据。

实验使用隔离的合成账户与冻结源码，不接触现役账户。必须先冻结再预检，每次正式运行使用新目录，不能覆盖已有付费结果。

```sh
python3 -B scripts/run_jev_ab.py freeze --out outputs/jev-ab/<新的目录>
python3 -B scripts/run_jev_ab.py preflight --out outputs/jev-ab/<新的目录>
python3 -B scripts/run_jev_ab.py run --out outputs/jev-ab/<新的目录>
```

S06 旧公开目录夹具与当前 HTTP 工具投影不兼容，必须修复后再作为质量基准；只通过离线固定值预检不足以证明模型取得了同一份证据。当前主模型仍存在终结工具合同与保守预算失败。保留旧结果，不能删失败、事后改阈值或把 GLM 同意算作答案正确。
