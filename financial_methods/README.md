# Navren 金融纯计算模块

合同版本：`1.0.0`。本模块只接收基座已核验的纯数据，不读取数据库、网络、模型或交易接口。

| 模块 | 入口 | 主要结果 |
|---|---|---|
| `contracts.py` | `MetricContext`、`FinancialMetric`、`money_metric` | 对象、时点/期间、口径、公式、证据、版本、覆盖与假设 |
| `funding.py` | `evaluate_funding`、`stress_funding` | 当前余量、覆盖期预算、确定/候选最低余量、规划追加与显式压力情景 |
| `accounting.py` | `balance_sheet`、`cash_statement` | 账面资产负债、现金收支与本金/投资现金流分类 |
| `returns.py` | `portfolio_equity`、`period_profit`、`reconstruct_history`、`simple_return`、`time_weighted_return` | 持仓与权益、移动平均成本损益、适用条件严格的收益率 |
| `allocation.py` | `arrange_funds` | 保留现金与按确认用途安排的两种规划，费用和资金守恒 |

人民币金额为整数分，真零与缺项分开。其他币种仅可处理基座明确最小单位的同币种整数加减及比率；价格乘份额的估值/历史重建第一版只支持人民币，不做汇率换算。所有业务缺项返回 `unavailable` 或 `infeasible` 及原因；非法合同构造或底层数值类型可抛 `ValueError`，服务边界应先做输入校验。

`FinancialMetric.to_dict()` 对应总控金融指标合同；收益率使用独立 `RatioMetric.to_dict()`，`unit=ratio`、`value_decimal`，不写入金额字段。输入引用和版本是调用方声明，模块验证一致性，不认证来源真实性或授权。

资金输入必须是未扣应急/冻结/留款的原始现金快照，事项须已由基座展开为有效结算日。`current_budget` 扣当前输入覆盖期的确定支出，不提前花未来收入；安排模块仅接受该预算，不重新扣底线。规划金额不创建真实留款或订单。

首版为新增合同，不替换旧服务的字段/函数。基座应添加受限映射、调用、结果适配并验证兼容；Agent绑定指标解释，UI原样显示，集成验收独立复算。合同变动更新版本，不静默改变单位、成本政策或期间。

详细口径、手算、限制和接线清单见 [交付报告](/Users/manton/CC/projects/Navren/outputs/window-progress/finance/report.md)。在项目目录复现：

```sh
python3 -B outputs/window-progress/finance/run_checks.py
```
