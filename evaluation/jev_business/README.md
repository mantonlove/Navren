# Jev 业务开发对照采集

只复用现存工程原问句，不表示真实客户分布或未见验证集。`cases.json` 保存原句、原件定位与 SHA，`oracle.py` 保存独立种子整数复算、官方 JSONP/固定发布逐字段复核和必要条件。GLM approved、HTTP 200、资料不足拒绝或关键词命中都不作为完整语义正确率。

当前 6 个普通候选：P01/P02/P04/P05/P07/P08。P03/P06 因 ORACLE_PENDING 排除；R01/R02 在独立 regression 组，不能混普通分母。P05 原问句无日期，正式链路无受信历史公共日期上下文，标 not_ready；禁止修改问句、patch_today、覆盖检索默认日期或用账户业务日伪装公共业务时点。

FinanceGateway 使用新的系统临时路径及演示 seed，账户 oracle 独立从期初和原始流水复算。公共源只读 SQLite backup 到冻结克隆，每次再新建临时克隆；撤回仅施加在 R02 克隆。规则复制到临时路径，预检预览不沿用到真实模型。本轮无需个人现役财务库，两个 arm 都关闭外部冷抓取。`records=()` 会使 public_products 的额外目录记录缺失；保留真实 provider.public_catalog、canonical_search 和观测到的 public_query_hints，不给模型 gold 工具路径。

冻结、预检、采集各使用新的 CLI 进程。下面命令由根任务在候选/方法复查和基线链路确认后执行；本开发交付没有发云调用。

```bash
python3 scripts/run_jev_business_ab.py --freeze --out outputs/jev-iteration-20261007/NEW-P01-PILOT --case-subset P01 --reps 1 --baseline-pilot-only --overlay-root outputs/jev-iteration-20261007/candidate-overlay --pricing-json outputs/jev-iteration-20261007/pricing-frozen.json
python3 scripts/run_jev_business_ab.py --preflight --out outputs/jev-iteration-20261007/NEW-P01-PILOT
/Users/manton/CC/services/cc-secrets/cc-with-secrets python3 scripts/run_jev_business_ab.py --collect --out outputs/jev-iteration-20261007/NEW-P01-PILOT
```

P01 单次基线 pilot 只是链路确认，不能称 AB 或开发门已过。后续 P04/P08 各 1 次配对 pilot：新目录、新冻结，`--case-subset P04 P08 --reps 1`，去掉 `--baseline-pilot-only`；默认两组的随机顺序和相邻重复 AB/BA 反转固定。6 题×2 重复的完整开发门仍缺 P05，上述 pilot 不替代它。

候选 overlay 必须提供 overlay.json，且唯一覆盖 agent_runtime/conversation.py；其 base_sha256 必须匹配当前正式源，overlay_sha256 必须匹配候选文件。记录候选与基础各自摘要。两组都为 agent_runtime.jev_agent.ReadPlanConversationAgent；同 progressive/105 秒/100k token/8 轮/12 工具，只改变 jev_policy='off'/'read_plan'。缺 supports_local_read_plans=True 时预检关闭云采集。

价格必须独立提供当日资料：schema_version=navren.jev-business-pricing.v1，verified=true，verified_at、sources、comparison_fx_cny_per_usd；rates.primary/reviewer/jev 各包含 model/provider/currency/input_range/output_range。无内置旧价格。计量为实际 provider usage 按冻结公开单价估算，不是账单；model/usage/单价未知立即停后续调用，不重发。费用上限 20 元，总 token 上限 4M，每次启动前保留下一次最大费用/token 额度。

collector 在派发前保存 collection-start、逐次 start；中断目录和已有 results 一律拒重跑。HTTP 往返包含 Jev、主模型、独立审核、最终守卫和发布；每阶段实际请求单独记录，本地计划不能计成主模型请求。记录相同 RecordingBankTools 观察子类的原始只读工具输出，super.invoke 返回不改，以免用云端匿名 ID 对 canonical ID 产生误判；云端真实请求和工具输出另存。两组使用相同观察方式。

必要条件检查与完整语义裁决分开。blind-review-pack 不含组别、耗时、Jev 选择、调用 ID 或本地计划来源字段，只保留题目、原始只读事实、正文及冻结 oracle；key 单独保存。独立全文复核前 semantic_pass 为 null。所有已启动失败留在分母；没有独立 holdout 时不能给最终宣传结论。

本地验证：`python3 -m unittest discover -s tests -p test_jev_business_harness.py -v`。早期 source-freeze-1 因遗漏 JSONL 原件未完成预检，其目录保留；source-freeze-2 修复并认证 fixture 门，但未含最新观察器/候选 overlay/价格，不能用于付费采集。最新无云 fixture 原件报告位于 outputs/jev-iteration-20261007/harness/fixture-preflight-current.json；根任务须按最终代码重新冻结。
