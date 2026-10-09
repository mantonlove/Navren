# Navren资产配置模型 — ERC 风险预算集成方案

> 历史设计或实施记录：保留文内注明版本的测试结论，不能作为当前版本的验收结果。当前实现与运行方式见 [README](../README.md) 和 [技术说明](../TECHNICAL.md)。本机机器信息已清理，未发布的历史产物不提供仓库链接。

## 当前状态

已完成：
- `_erc_weights()` 替换固定比例模板（`agent_core.py`）
- `RISK_BUDGET` 按等级约束（`agent_core.py`）
- DOCX 参考文献文档（18 篇，学术排版）
- `calculate_allocation()` 返回 `method` 字段标注理论来源

未完成（需要接上）：

## 改动 1：LLM prompt 注入方法论

**改 `_build_allocation_prompt`**

现在 LLM 的 prompt 中没有告诉它"为什么是这个比例"。需要注入：

```
配置方法论：基于 ERC（Equal Risk Contribution）风险预算模型计算。
你的 R{level} 风险等级允许最大组合波动率 {max_vol}，权益仓位上限 {max_equity}。
各产品的权重由 w_i = (1/σ_i) / Σ(1/σ_j) 确定，即波动率越低的产品分配越高权重。
这个方法的理论依据是 Maillard, Roncalli & Teïletche (2010) 的 ERC 框架。
请在回复中用 1 句话解释：为什么固收类占比高/低。
```

**效果**：AI 回复会解释"根据风险预算模型，R2 等级的组合波动率控制在 3% 以内，因此固收类占比较高"。

## 改动 2：推荐页展示配置方法论

**改 `recommend.js` `renderRecommendations`**

在"方案依据"区域增加一行：

```
配置方法：ERC 风险预算（Maillard et al. 2010）
组合波动率估计：{est_portfolio_vol}
风险预算上限：{max_vol}
```

**数据来源**：`qingzhou_allocation` 中已有 `method` 和 `est_portfolio_vol` 字段（`calculate_allocation` 返回的）。

## 改动 3：更高收益诉求反推

**改 `_build_higher_return_intercept`**

现在用 `EXPECTED_RETURN_BY_RISK` 做简单查表判断"目标收益是否可达"。

改进：用 ERC 反推。

```
输入：用户目标年化收益 target_return（如 6%），当前风险等级（如 R2）
反推逻辑：
1. 查 ASSET_VOLATILITY：要达到 6% 收益，需要 R4 等级的权益敞口（波动率 ~16%）
2. R2 的 max_equity = 0.10，权益仓位只能 10%，隐含收益上限 ~3.5%
3. 6% > 3.5% → 目标收益在当前等级不可达
4. 反推需要的最小等级：哪个等级的 max_equity * R4_expected + (1-max_equity) * R2_expected >= 6%
   → R4: 0.60 * 8% + 0.40 * 3.5% = 4.8%+1.4% = 6.2% ≥ 6% → 需要 R4
5. 输出：您当前 R2 等级无法达到年化 6%，至少需要 R4 进取型。建议重新测评。
```

**效果**：不只是说"目标偏高"，而是给出具体的"需要 R4 等级才能达到 6%，因为 R4 允许 60% 权益仓位"。

## 改动 4：`chat.js` `addBookmarkButton` 接上 saveRecommendationSnapshot

收藏时检测 `result` 是否含 products/allocation → 调用 `saveRecommendationSnapshot`。

## 改动 5：`chat.js` init 补上 `action=handoff` 分支

`params.get('action') === 'handoff'` → 调用 `triggerHandoff`。

## 改动 6：`recommend.js` 产品名 onclick 转义修复

产品名含双引号时 `onclick` HTML 属性断裂 → 改用 `encodeURIComponent`。

## 涉及文件

| 文件 | 改什么 |
|------|--------|
| `agent_core.py` | `_build_allocation_prompt` 注入方法论；`_build_higher_return_intercept` ERC 反推 |
| `recommend.js` | 方案依据加配置方法论行 |
| `chat.js` | addBookmarkButton sync + handoff 分支 |

## 验证

1. 聊天发"帮我配置"→ AI 回复中解释"基于 ERC 风险预算……"
2. 收藏 → 推荐页出现方案 → 方案依据区显示配置方法论
3. "我希望年化 6%"→ AI 回复"当前 R2 无法达到 6%，需要至少 R4……"
4. 账户页点"人工客服"→ chat 自动 handoff
5. 产品名含引号时"发送至聊天"不崩溃
