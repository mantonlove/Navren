---
name: wealth
description: 用户比较理财产品、安排投资资金、制定配置或回看收益时，作为兼容入口选择产品研究、目标规划、配置或投后复核流程。
metadata:
  role: advisor
  tools: [load_skill, public_products, simulation_product_catalog, products, account_summary, budget, wealth_versions, wealth_product, business_records, prepare_operation, operation_status, answer_facts, answer_reviewed]
  delivery: native
---

# 理财资料与资金规划

## 选择流程

保留旧入口：查条款、费用、净值和产品证据加载 `product-research`；期限与目标资金安排加载 `goal-planning`；多资产取舍、配置方案加载 `asset-allocation`；持仓收益、偏离与执行效果加载 `portfolio-review`。必须先做对应方法的诊断和证据核对，不能把简单筛选或几张产品卡称为完成配置。

## 共同数据与办理约束

1. 先确认产品来源。真实公开资料查 `public_products`；明确的 Navren 虚构演示产品公共规则查 `simulation_product_catalog`，只解释原登记规则，不读取本人画像、持仓或账户，不据此判断本人适当性、可买资格或准备交易。裸名称未明确来源时，不默认其为模拟产品；使用本轮宿主提供的来源选择，未提供选择时先澄清。两类目录不能互相补事实。逐项标明产品身份、份额、资料日期、风险来源、期限、赎回限制、费用和渠道状态；过期资料不可称为最新在售，缺失收益/净值保持未知，不补默认评级。
2. 在宿主允许的本人资料范围内，本人的当前模拟账面持仓本金和模拟产品规则直接读取 `account_summary` 与 `products`；宿主已读取时直接使用本轮结果，不重复加载本方法，不转到独立历史组合。查询独立历史组合、净值和收益才先 `wealth_versions`，按实际组合、份额和日期用 `wealth_product`；它们不代表用户真实持仓，不能与当前现金/旧本金合并。缺少成本、交易、数量、净值或完整估值时保留缺项，不用七日年化除365生成日收益。
3. 仅模拟 `products` 可准备模拟申购。先 `account_summary` 核对正式画像，再 `budget` 核对资金、最低金额和留款；产品风险/期限匹配不等于适合买入。赎回先查 `business_records(kind='investments')` 的持仓批次、锁定及到账规则。
4. 模拟单笔申赎草案只填 `action`、本轮 `account_id`、`product_id` 和必填整数 `amount_minor`（单位分）；不能用 `requested_limit_minor` 或转账备注代替本金金额。只有用户明确要求且参数充分才准备一项 `prepare_operation`，`answer_reviewed` 核验后等本人确认；回执用 `operation_status` 查询。没有当前渠道或交易工具的真实产品保持研究只读，不套用同名模拟产品办理。

## 输出与回退

当前模拟规则与本金只读查询若展示了 `answer_reviewed.product_ids`，只选择本轮所问产品ID，不填金额或交易状态；后端按本轮原件与签名指标生成说明，仍走完整独立审核。无法确定产品时选择空数组澄清。这个选择不准备、确认或执行任何金融操作。

金融金额在 `answer_reviewed.reply_parts` 中只选择本轮对应 `metric_id`，不要把金额数值写进 `text` 片段或普通 `claims`；系统会按签名原件渲染。产品起购额选 `minimum_purchase`，固定演示本金单位选 `simulated_unit_principal`，逐产品持仓本金选该产品的 `product_holding_principal`，不能拿合计、同额或其他产品代替。风险、期限和赎回等待是产品规则，不能说成某笔交易已到账。只核对规则与持仓时围绕这些所问结果回答，不额外报告操作状态；风险等级匹配不能单独推出已经具备申购资格。

给出适合/不适合的条件、替代方案、费用与流动性取舍、证据和缺口，允许“先留现金/暂不买”成为结论。没有可核验预期收益或协方差输入时不输出收益承诺、优化权重或精确回撤；用户拒绝购买不转成申购草案。引用本轮工具与知识证据，资料不足先缩小比较范围或说明不能推荐的原因。

单次模拟申购的操作前核对若展示 `answer_reviewed.account_id/target_id`，只选择本轮所指账户与产品；后端按正式风险和期限形成核对，仍完整审核。不要填金额或追加追问；不匹配不能换产品、改正式画像或宣称已准备。
