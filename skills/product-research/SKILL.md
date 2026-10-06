---
name: product-research
description: 用户要研究或比较理财、基金、存款产品，核查费用、期限、风险、退出和当前渠道时，建立逐产品证据与适配条件，避免只按收益或评级排序。
metadata:
  role: advisor
  tools: [load_skill, knowledge_status, knowledge_search, public_products, products, wealth_versions, wealth_product, account_summary, budget, answer_reviewed]
  delivery: adapted
---

# 产品研究

方法适配自 `anthropics/knowledge-work-plugins` 的 `search-strategy`、`knowledge-synthesis` 和 `validate-data`：保留问题拆解、原文归因、比较口径与缺项核验。版本/许可由 `skills/upstream/provenance.json` 登记；中国银行产品身份、收益口径和适当性规则由 Navren 维护。

## 触发与必要读取

用户问产品是否适合、两者差异、费用退出、风险来源或资料新鲜度时使用。公开产品先 `public_products`；需要条款原文时加载 `knowledge-retrieval`，只查询已注册且允许云披露的公开 provider。历史净值/份额详情先 `wealth_versions` 再 `wealth_product`，保留真实 `portfolio` 与日期。`products` 仅是可执行业务规则的模拟产品，不能补充公开市场事实。

## 分析顺序

1. 确认管理人/发行人、产品代码、份额、币种、销售渠道和适用日期。A/C 份额、不同渠道、历史与现售资料分别研究，不按名称拼成一款产品。
2. 核查产品机制与底层风险、风险评级来源、期限/锁定、最低金额、开放/暂停条件、赎回确认与到账，以及费用/计提方式。未知底层权重不生成集中暴露或损失估算。
3. 分开净值历史、货币基金万份收益、七日年化、业绩比较基准与承诺收益。七日年化不能除以 365 当每日收益；比较基准不等于投资者实际收益。
4. 费用区分净值内运作费用与申赎/渠道收费；不能重复扣费，渠道折扣未核验就保持未知。不能凭“产品为基金”猜具体费率或交易日规则。
5. 对每项拟用结论检查原文位置、版本/效期、主体/份额/渠道和来源状态；历史销售关系不证明当前可买。无法证明当前渠道则只作研究，不形成新申购依据。
6. 个性化适配才读 `account_summary` 的正式画像与 `budget` 的当前资金，结合已确认期限和流动性需要。资料不足时给附条件判断与排除理由，不按最高历史收益替用户选。

## 用户输出

先给适用/不适用条件和关键差异，随后解释风险、退出、费用及待核验项，附必要原文入口。用 `answer_reviewed`，产品事实逐项引用本轮证据；历史净值明确截至日和研究范围，不声称已接实时在售行情。

## 降级与授权边界

官方目录过期、条款检索未接入、OCR/附件未解析、证据冲突时标明受影响字段；不造净值、额度或费用。研究不准备买入、不重命名原有持仓、不更新正式风险画像。用户明确购买时另加载 `wealth` 并查最新模拟产品/预算，沿用独立确认链。

## 按需参考

比较两款产品或处理收益/费率口径时，调用 `load_skill(name="product-research", reference="product-evidence.md")`。
