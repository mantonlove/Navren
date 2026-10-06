---
name: wealth
description: 用户比较理财产品、安排投资资金、制定配置或回看收益时，作为兼容入口选择产品研究、目标规划、配置或投后复核流程。
metadata:
  role: advisor
  tools: [load_skill, public_products, products, account_summary, budget, wealth_versions, wealth_product, business_records, prepare_operation, operation_status, answer_facts, answer_reviewed]
  delivery: native
---

# 理财资料与资金规划

## 选择流程

保留旧入口：查条款、费用、净值和产品证据加载 `product-research`；期限与目标资金安排加载 `goal-planning`；多资产取舍、配置方案加载 `asset-allocation`；持仓收益、偏离与执行效果加载 `portfolio-review`。必须先做对应方法的诊断和证据核对，不能把简单筛选或几张产品卡称为完成配置。

## 共同数据与办理约束

1. 真实公开资料先查 `public_products`。逐项标明产品身份、份额、资料日期、风险来源、期限、赎回限制、费用和渠道状态；过期资料不可称为最新在售，缺失收益/净值保持未知，不补默认评级。
2. 持仓与收益先 `wealth_versions`，按实际组合、份额和日期用 `wealth_product`；独立历史组合的真实管理人净值不代表用户真实持仓，不能与当前现金/旧本金组合合并。缺少成本、交易、数量、净值或完整估值时保留缺项，不用七日年化除365生成日收益。
3. 仅模拟 `products` 可准备模拟申购。先 `account_summary` 核对正式画像，再 `budget` 核对资金、最低金额和留款；产品风险/期限匹配不等于适合买入。赎回先查 `business_records(kind='investments')` 的持仓批次、锁定及到账规则。
4. 只有用户明确要求且参数充分才准备一项 `prepare_operation`，`answer_reviewed` 核验后等本人确认；回执用 `operation_status` 查询。没有当前渠道或交易工具的真实产品保持研究只读，不套用同名模拟产品办理。

## 输出与回退

给出适合/不适合的条件、替代方案、费用与流动性取舍、证据和缺口，允许“先留现金/暂不买”成为结论。没有可核验预期收益或协方差输入时不输出收益承诺、优化权重或精确回撤；用户拒绝购买不转成申购草案。引用本轮工具与知识证据，资料不足先缩小比较范围或说明不能推荐的原因。
