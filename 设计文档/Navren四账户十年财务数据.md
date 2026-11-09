# Navren 四账户十年个人财务数据

本轮只构造财务数据，账户称号为：A市首富、种粮大户、坚持上班的包租婆、退休后重新创业的阿姨。没有人物背景、口头禅、预设对话或故事文本。

## 文件与覆盖范围

主文件：[financial_data.sqlite3](/Users/manton/CC/projects/Navren/outputs/contest-finances/financial_data.sqlite3)。输出目录：[contest-finances](/Users/manton/CC/projects/Navren/outputs/contest-finances)。

日期为 **2016-10-04—2026-10-03，闭区间，精确十年、3652天**，与当前模拟银行参考日期一致。每账户有3652份日终快照；覆盖121个日历月桶及11个日历年桶，首尾月、首尾年明确标记为不完整期间。不能把2016或2026的局部期间当作全年比较。

四个稳定用户ID：`tycoon`、`grain_farmer`、`working_landlady`、`retired_entrepreneur`。所有账户、分录、流水和资产均绑定自己的用户ID。

每个用户目录提供：

- `cash_transactions.csv`：现金账户逐笔流水，含正金额、带符号变动额、分类、资金用途、分录关联和内部转账配对编号。
- `daily_balances.csv`：逐日现金、金融投资、非金融资产、负债、总资产、净资产。
- `asset_values.csv`：逐日资产账面价值、成本和价值与成本的差额。
- `loan_balances.csv`：逐日贷款剩余本金。
- `monthly_summaries.csv`、`annual_summaries.csv`：收入、净费用、退款、结余、现金流量、期初期末现金、已实现投资损益、未实现估值变动。
- `current_finances.json`：覆盖期末的现金账户、资产、负债本金、财务参数及当前财务快照。另有周期支出预测，明确为未付款预测。

共享文件 `summary.json` 为行数与期末总量；`data_contract.json` 为实际数据库字段合同；`audit_report.json` 为独立审计结果。

## 数据内容与参数

| 账户 | 收入结构 | 主要资产及负债 | 记录的阶段变化 |
|---|---|---|---|
| A市首富 | 个人持股分红、住宅与商铺租金、存款利息、已实现投资损益 | 个人股权、物业、三类合成投资、个人物业按揭 | 贷款逐期偿还；投资申赎、估值和现金调拨 |
| 种粮大户 | 两季粮食销售回款、农机服务回款、合成经营补贴 | 住宅、农机设备、三类合成投资、设备贷款 | 土地租赁和农资投入早于季节回款；设备折旧及贷款偿还 |
| 坚持上班的包租婆 | 工资、三处出租住宅租金、存款利息、已实现投资损益 | 自住及出租物业、三类合成投资、两笔按揭 | 工资和租金基准变化；按揭、维修、投资与账户调拨 |
| 退休后重新创业的阿姨 | 前期工资、2020年7月起退休金、2023年起个人自营回款 | 自住住宅、三类合成投资、住房贷款 | 工资停止、退休金开始、经营收入与费用开始；开店筹备支出有结束日期 |

风险等级、投资期限和储备金额是人工设定的演示参数，不代表完成正式测评。个体经营资金标记为 `self_employed`，家庭资金为 `household`，个人用途为 `personal`。首富只记录个人持股估值和个人分红，不混入被投资企业营业额、公司账户或公司负债。

## 金额和会计口径

金额统一为**整数人民币分**。阅读展示除以100；CSV以UTF-8 BOM编码导出。数据库中的借方为正、贷方为负，所以负债科目原始余额为负；`loan_balances.principal_minor` 和期末 JSON 的 `outstanding_principal_minor` 则明确为正的未偿本金。

1. 每事件分录借贷平衡，期初资产与负债独立登记；覆盖期前的累计净资产进入期初科目，不补造覆盖期前交易。
2. 日终现金、资产和贷款余额从分录计算。总资产＝现金＋金融投资＋非金融资产；净资产＝总资产－负债。
3. 收入取已确认收入科目的净贷方，排除未实现估值；已实现投资损益单独列出，亏损允许为负。
4. 费用取费用科目的净借方，退款冲减原费用；设备折旧属于非现金费用，未混入现金消费。
5. 投资申购、赎回本金、贷款本金和本人账户转账不算收入或消费。内部转账保留两条现金腿，汇总的现金流入和流出包括这两腿，净额为0；不将现金毛流量称为个人收入。
6. 投资赎回按剩余账面价值比例减少成本，将对应已实现损益从未实现科目重新分类，避免重复确认收益。
7. 支出前若日常账户现金不足，明确记录其他本人账户的调拨；仍不足则记录投资赎回。不能截零余额或制造外部补款。
8. 季节性参数中的金额为年度总额，按日历年增长后分配至约定月份；月度基准支出和额外随机个人消费分别记录。

## 来源和实际边界

全部财务数据均为 `navren_synthetic`，包括人物称号账户、回款、费用、利率参数、资产价值和收益路径。期初投资成本假设等于期初价值；金融投资按自然日合成估值，其他资产按月合成估值，未采用真实交易日历。设备价值与成本差额包含折旧，不能将其当作基金浮亏。

**这不是银行真实客户数据，也不是微众银行或真实基金的历史收益。** 没有使用真实机构产品名称填充合成收益。参数未经真实客户或市场分布校准，外部真实性及外部总量核验不在本次内部对账通过范围内。

`recurring_obligations` 的期末之后日期是预测，状态为 `forecast_not_paid`，不进入十年已发生流水和汇总。`financial_targets` 仅表示合成储备目标记录，不表示冻结或已经支付。

本轮未修改当前服务数据库，也未接入登录或账户切换。现有服务的通用单笔校验上限为100万元，银行年报入口从2025年起；CSV入口每批最多5000行、1.5MB，且不会更新银行余额或持仓。这里的CSV为完整财务数据合同导出，**不声称可直接通过现有账单上传界面导入**。后续应绑定四个独立工作区，分别接入各自的历史、持仓、记忆、目标和操作记录。

## 复现与审计

生成参数：[contest_profiles.json](/Users/manton/CC/projects/Navren/simulated_bank/fixtures/contest_profiles.json)，固定随机种子为20261005。

```sh
python3 scripts/generate_contest_finances.py
python3 scripts/audit_contest_finances.py --db outputs/contest-finances/financial_data.sqlite3 --output outputs/contest-finances/audit_report.json
python3 -m unittest discover -s tests -p test_contest_finances.py -v
```

独立审计器不调用生成器的汇总函数，直接核对分录、现金腿、资产负债、日终回放、期间加总、用户归属、主键、字段值、金额类型与日历覆盖。审计结果以 `audit_report.json` 的通过项和未覆盖项为准，失败返回非零退出码。

## 当前产物规模

| 账户 | 用户ID | 现金流水条数 | 日终快照条数 |
|---|---|---:|---:|
| A市首富 | tycoon | 4664 | 3652 |
| 种粮大户 | grain_farmer | 8602 | 3652 |
| 坚持上班的包租婆 | working_landlady | 4957 | 3652 |
| 退休后重新创业的阿姨 | retired_entrepreneur | 4754 | 3652 |

合计22977条现金流水、65616个财务事件、132026条借贷分录、83996条资产价值记录、18260条贷款余额记录、484份月汇总、44份年汇总。

## 本轮验证结果

独立审计9项检查通过，覆盖分录平衡、逐事件非负资产、日终余额、成本回放、投资损益重分类、现金腿、期间汇总、用户归属、唯一键、必需值和时间语义。九类审计中8类覆盖，外部总量核验（external_total）未覆盖：合成数据没有真实客户或行业总量基准。附加检查通过SQLite完整性、预测独立状态和储备目标值域。

13项回归测试全部通过，包括正常数据验证和故意破坏分录、余额、成本、汇总、现金腿、用户归属及日期后的失败检测；运行时将 Python 警告作为错误处理。

打包文件：[Navren四账户十年财务数据.zip](/Users/manton/CC/projects/Navren/outputs/contest-finances/Navren四账户十年财务数据.zip)，包含四用户数据、SQLite数据库、参数、字段合同、审计结果、说明和生成／审计脚本。压缩包中的 `manifest.json` 记录文件SHA-256校验值。

解压后如需重新生成，明确指定参数文件：

```sh
python3 tools/generate_contest_finances.py --profiles contest_profiles.json --output regenerated
python3 tools/audit_contest_finances.py --db regenerated/financial_data.sqlite3 --output regenerated/audit_report.json
```
