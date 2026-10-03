# 第三方来源与改编说明

这些资料用于模拟零售银行数据建模，不能证明Navren接入真实银行、得到赛事官方数据库或获得任一机构认可。

| 来源 | 固定版本 | 实际用途与保留内容 |
|---|---|---|
| [Apache Fineract](https://github.com/apache/fineract) | 4684c663805560d5b6948045224440441fa4485c | 从官方SQL样本提取客户、存款、贷款、还款计划等结构，适配到SQLite；保留7条官方示例客户的6个非敏感字段，单独存放于reference_examples。不是完整Fineract部署。 |
| [IBM Synthetic Data Sets](https://github.com/IBM/IBM-Synthetic-Data-Sets) | 4cbd5aab7d8a18600ee0f4b622c225d44918ecd1，schema v1.1.0 | 保留核心银行和支付卡共10份官方结构文档，参考客户、卡片、交易、金融机构和账户关联。仓库公开的是结构文档，未取得完整商业数据包。 |
| [Open Banking UK Read Write API](https://github.com/OpenBankingUK/read-write-api-specs) | 484a1795680d9852600247878b9fd2bfe584c0aa，4.0.1 | 参考账户、收款人、月结、定时付款、代扣和授权等资源概念。项目内只保留资源名和版本记录，未复制标准全文或客户数据。 |

Fineract与IBM仓库为Apache-2.0；随包保留其LICENSE文件，Fineract另保留NOTICE_SOURCE。`reference/fineract-selected-models.json`是从Fineract SQL提取的列名与DDL片段，`reference/fineract-client-examples.json`是筛选后的官方示例客户。改编包括SQLite类型、工作区隔离、人民币整数分、脱敏显示及本项目新增业务字段。IBM文档按原路径保存于`reference/ibm/`。

Open Banking仓库README标注Open Licence；这里不把它视为Apache许可、不复用接口代码。此前找到的Open Banking Open Licence PDF涉及开放参考/产品数据，不能据此推定所有个人银行数据或源代码的再许可。需要完整复制标准时应重新核验适用许可。

`reference/upstream-manifest.json`保留下载的固定提交URL、字节数与SHA256；调研原件位于`outputs/模拟银行验收/第三方调研/`。完整下载SQL仅供本地研究，未执行，也未将其中应用用户的示例密码导入服务。

所有Navren演示客户、联系人、账户、商户、金融产品、档案金额及任务均为原创模拟数据，`data_origin=qingzhou_synthetic`。实体的`source_id`指结构依据，不是该行金额的来源。每张表的`schema_reference`表示实体概念适配，不声称逐字段等价；直接字段映射另见覆盖说明。`reference_examples`的`data_origin=upstream_synthetic_sample`仅用于展示真实来源链，不能并入客户的实际账本。

调研但未采用：Open Bank Project（AGPL-3.0/商业双许可，未复制其实现或样本）；IBM TabFormer（侧重信用卡交易数据，不能独立支撑完整客户档案）。
