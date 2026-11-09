# 本地共享证据库

独立 SQLite 保存权威原文、版本、准入、不可变发布快照和公共证据图。FTS5 是词法候选入口；可返回的候选必须再通过产品／份额／渠道、图路径、来源许可、原文定位、有效期、字段时效和冲突检查。没有云端嵌入、生成式关系抽取或用户资料入库。

## 初始化与离线更新

```python
from local_knowledge import LocalKnowledgeProvider

provider = LocalKnowledgeProvider("/明确配置目录/public-knowledge.sqlite3")
# 构造只初始化数据库，不访问网络。空库 ready=False。
# market 是已建立的 MarketStore；只读其缓存，不调用它的更新接口。
report = provider.import_market_store(market, publish=True)
# report: imported / rejected / ready / count / version_ids / rejections / release_id
```

`NAVREN_KNOWLEDGE_DB` 可覆盖默认目录；服务应注入独立路径。现有含个人数据库标记表的文件会拒绝初始化。数据库文件权限为 0600；本模块的写入／注册／准入方法只供受信本地应用使用，不能作为模型任意写入工具。

```sh
python3 scripts/sync_local_knowledge.py --database /明确配置目录/public-knowledge.sqlite3 --market-database /既有目录/market.sqlite3 --publish
```

启动不自动发外网请求。离线同步命令需要显式 `--webank`；已认证 HTTP 聊天在命中微众公开对象或目录问法时，可通过下述公共证据运行时读取同一个固定端点，不向来源发送用户提问。不恢复被撤销的来源权限。Webank 的原始 JSONP 单独留存，记录和字段有解析表示、哈希、精确定位。产品标识是微众渠道披露代码；未验证底层产品／份额映射，披露销售状态不等于即时可买，未知终止日期保持 `None`。新字段或类型漂移暂停准入，需审核适配器和调用 `acknowledge_structure` 后再同步。

## 候选、审核与发布

```python
version = provider.stage_document(
    source_id="已登记源", source_url="已登记HTTPS地址", title="标题",
    text=original_text, raw_bytes=original_text.encode("utf-8"),
    parser_id="exact-text/1", shape="本地审核过的结构版本",
    published_on="2026-10-01", retrieved_on="2026-10-06",
    product_id="已确认公共产品", facets=["fees"],
)
# 必须先登记来源许可、字段时效与明确公共身份，再由受信应用审核原文。
provider.approve_version(version, verification_basis="可追溯本地审核记录")
release = provider.publish()
```

`stage_document` 仅产生候选。严格的既有官方缓存适配器可以按登记合同准入；维护者描述、合成数据、元数据和残缺正文不能准入。准入新版本会立即撤销旧版本在修订生效日期之后的可用性，既有快照也受实时撤销门禁约束；历史版本不删除。撤销新版本不会自动恢复旧版本。冲突作用于指定维度和日期；包含冲突／过期事实的整个片段不能外发，应按独立字段切片以保留无关证据。

发布是增量、内容寻址的不可变快照；同内容重复发布返回同一代次。消费者每轮固定代次，不能悄悄切换。`publish(version_ids=[version], temporary=True, activate=False)` 与 `for_release` 可以创建专用会话证据快照，但仍要求相同准入，不会自动晋升共享当前版本。

两个官方导入器在显式 `publish=True` 时，只将本次明确核验版本加入当前正式封存成员；不会夹带其他已准入但未发布版本或专用临时快照。合并在同一 SQLite 写事务内进行，空导入或版本均已在当前成员时不换代次。`provider.publish()` 无参数仍是受信管理人员显式发布全部准入版本的接口；不要用于启动导入。导入只决定发布成员，已过期成员不会因此恢复当前财务证据可用性。

## 检索与本地图审计

```python
import json
from pathlib import Path

release = provider.status()["public_release_id"]
request = {
    "scope": "public", "query": "费用和赎回", "as_of": "2026-10-06",
    "product_id": "已确认公共产品", "share_class_id": None, "channel_id": None,
    "facets": ["fees", "liquidity"], "limit": 4,
}
result = provider.search_public(request, release)
# 保留 schema_version / release_id / citation_ids / coverage_gaps；retrieval 增加本地召回诊断。
evidence = [provider.resolve_public_evidence(cid, release, request["as_of"])
            for cid in result["citation_ids"]]
audit = provider.graph_search(request, release)
Path("/明确本地审计目录/graph-trace.json").write_text(
    json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
```

`graph_trace(citation_id, release_id, as_of, request=request)` 返回固定路径和精确原文定位。路径包含产品／份额／渠道 → 条款 → 对应规则节点，及条款 → 文档版本 → 文档 → 来源。每条边绑定同一引文版本，并在查询时重查。搜索最多四跳，单代最多 5000 片段，单次最多 8 条证据；超出规模需另行扩展，不能宣称已经验证大规模服务。

自动创建的是经过准入的资料绑定和原文映射，规则节点暂存未结构化引文。发行／管理／分销等金融关系只能通过 `add_reviewed_relation` 明确人工审核录入；接口核对主体、类型、原文和标签。没有这些事实就没有机构关系，不从产品名、词语共现或图边名称推断。`related_products` 是有双边来源的两跳同发行机构查询，不推出投资质量、适当性或可买性。

`public_catalog()` 返回曾经准入并正式发布的公共对象／已确认概念，合计最多 5000 项，供出站净化注册；来源须当前仍获出站授权，主体和原文仍须验证。稳定公开身份不按条款时效消失，历史时点查询可以传入该身份；财务证据的有效期仍由检索和回查严审。候选、未发布或专用临时快照不加入这个共享允许名单。`status()` 提供实际图实体数、已发布有证据关系数、检索方法及模型能力状态；`ready` 表示有发布快照，不表示当日所有任务都有充分证据。

## 按需补充

`SupplementService.register` 是受信配置入口，绑定已登记官方／授权源、固定端点、公共产品／份额／渠道和维度。`supplement(request)` 只把登记对象字段交给 `FetchRequest`，没有用户原问题、历史、持仓或账户字段。对象键有独立公共允许名单；不能用任意私人标识拼接来源请求。回调属于受信代码，必须使用来源白名单和读取预算。

补充有同键合并、正／负缓存、至多两个任务线程、四个等待键、每次两个来源／四份文档及字节预算；缓存键包含端点、来源修订和主体。迟到结果不准入，失败返回具体缺口，不空覆盖好版本。固定端点读取的同步调用有总截止，阻塞 DNS／读取最多占两个专用槽；超时返回并丢弃迟到正文，不承诺操作系统已取消 DNS。补充只产生候选，不自动审核、发布或更换调用者已固定的代次。

目前实现 `exact-text/1`、微众严格 JSONP 和固定版本的 PDF 文字提取三个非生成式解析器。PDF不自动解释条款或识别表格的金融含义。本地向量与 FTS5 混合召回已接入；金融费用计算和机构关系仍须各自有依据。共享图与个人图独立；本模块不检索或输出个人证据。

## 本地向量混合召回

可选依赖在 `local_knowledge/requirements-vector.txt`，不覆盖根依赖。先显式安装与准备公开模型：

```sh
.venv/bin/python -m pip install -r local_knowledge/requirements-vector.txt
.venv/bin/python scripts/provision_local_embedding.py
.venv/bin/python scripts/build_local_vectors.py --encoder bge --database /明确配置目录/public-knowledge.sqlite3
```

原中文单路模型为 BAAI 中文 `bge-small-zh-v1.5` 的固定 Xenova ONNX q8 导出；3 个文件共约 24.45 MB，固定 revision 和 SHA256。仅在准备脚本下载，查询端通过本地文件的 Tokenizers + ONNX Runtime CPU 运行，不调用 embedding API、不运行生成式模型、不记录原问题。查询附官方检索指令；原片段按 512 token、64 token 重叠滑窗，超过 8 窗明确拒绝，尾部不会静默截断。设置 `NAVREN_EMBEDDING_DIR` 指定模型目录，`NAVREN_VECTOR_ENABLED=0` 显式关闭向量分支。

`vector_releases` 与 `chunk_vectors` 位于公共 SQLite 内，均是可重建派生索引；权威原件、引文、发布和图谱继续使用原表。索引绑定封存快照、模型 profile、版本化检索表示 hash、向量 hash 和完整清单。已封存的新版本与启动时的当前快照在模型就绪时由单个后台线程建立索引（最多 8 个排队任务）；查询不建索引，未就绪/损坏/模型缺失时继续 FTS5，并在 `retrieval.vector_reason` 与 `status.vector_index` 标原因。恢复后可用显式建索引命令重试。

读取顺序：固定发布成员 → 原文/身份/权限/时间/图路径核验 → 合格候选的向量与关键词独立排序 → 加权 RRF 融合 → 维度覆盖 → canonical resolver。向量中不含个人资料；向量分数不是事实置信度、购买资格、投资价值或答案支持。近邻只保证相对主题接近，具体事实缺失时须按既有证据/金融指标合同拒答或说明缺项。

当前每个发布快照最多 5000 片段，采用 NumPy 精确余弦，不启动 Qdrant 等额外服务；尚未验证银行大规模吞吐。最近最多 8 个完整校验通过、同模型 profile 的历史索引可按相同表示 hash 复用；新版 citation 仍独立绑定新版原文，许可、撤回和图关系实时回查。历史索引保留，不自动删代次。详情见 `设计文档/Navren本地向量检索实施与验收.md`。

`vector_documents.py` 按受信 parser、shape 和来源选择表示。微众完整单字段 JSON 附官网中文字段名与渠道披露代码；标题、原值及 JSON 引文不改。字段身份或定位不匹配、字段未知或续片不完整时仅保留原片段，不猜标签。市场完整原文保持原语种；经审核 PDF 保持原文和既有页码定位。表示政策 `public-vector-documents/2` 纳入输入 hash，旧政策索引明确降级，需重建派生表。

自动增量共享发布保留其他文档，但剔除已经到达确认替代日期的旧版本，防止每日更新在当前快照内不断累积；尚未到达替代日期的旧版本仍保留。已封存历史快照、原文、图和向量不修改，历史问题应固定到原来的发布代次。重复同步不得以已被替代的旧版本覆盖当前发布。

“详见产品页面”“详见产品页”“以保险公司核定为准”仅标记为引用其他资料，不能当作已有费率。建索引结果报告各资料类别、字段数、占位数、新编码与复用片段数；`retrieval.vector_used` 说明本次是否实际有向量候选。净值、收益、事件序列与个人资料不走这一文本向量适配。完整微众目录超过单快照片段上限，本次仅验证代表性样本，不宣称全量容量通过。新验收见 `设计文档/Navren向量数据适配与验收.md`。

## 字段与跨语检索修复

默认公共向量选择为 `dual`：E5 与 MiniLM 本地 CPU 量化编码器使用各自384维数学空间。两者掩码平均池化与 L2 归一化，但前缀、窗口政策和模型哈希不同，分别建索引。E5原问题与术语投影是基础路径；跨语且固定词表覆盖不足一半或无投影时追加MiniLM原问题分支。原 BGE 的编码入口及512维索引保留，设置 `NAVREN_VECTOR_ENCODER=bge|e5|aligned` 可明确选单路；不同 profile 的向量不能互相比较。模型缺失、依赖缺失或索引尚未就绪时返回明确分支原因；一条可用则保留其候选并标记 `vector_partial`，全部失效则词法降级，查询不下载模型、不建索引。新版本后台索引仍使用单线程有界队列。

公共字段检索先核实原文、来源许可、日期、身份和图路径，再使用严格字段表示识别所问维度。完整公共名称及带命名空间的渠道披露代码可用于对象消歧；裸数字不能当基金或渠道身份，同名多对象须澄清，文本对象与明确选中对象冲突时不给引文。产品名称从字段检索问法中移除，防止同标题的所有字段获得重复词法票；不会据此推断份额或购买资格。

中英文资料使用有限的公共金融术语表生成本地查询特征，原问题与术语投影各自检索，各词法／语义视图独立排序后用固定总权重 RRF 融合。额外模型只按原问题检索，各空间分别应用相似度门槛；跨空间不比较余弦，全部语义视图仍共用总权重1.25。固定词表汉字位置覆盖比例只作本地路由特征，重叠不重复统计，不作事实可信度。不同查询的 BM25 和余弦均不直接混加；弱词法覆盖折减、零词法票不补入结果。术语表只改善覆盖范围内的问法，不保证任意跨语问题，不能当作原文翻译、证据或新的图谱事实。枚举问题可补入同一已核验版本最多两个连续邻近片段，跨版本、份额、渠道、缺口及比较多报告的扩展被禁止。

```sh
.venv/bin/python scripts/provision_multilingual_embedding.py --help
.venv/bin/python scripts/provision_aligned_embedding.py --help
.venv/bin/python scripts/build_local_vectors.py --database /已核验公共库路径
PYTHONPATH=.:tests NAVREN_VECTOR_ENABLED=0 .venv/bin/python -m unittest tests.test_retrieval_shortfalls
.venv/bin/python scripts/verify_retrieval_shortfalls.py --suite regression --split all --encoder dual --output outputs/retrieval-shortfalls/新的回归.json
.venv/bin/python scripts/verify_retrieval_shortfalls.py --suite holdout2 --split test --encoder dual --output outputs/retrieval-shortfalls/新的独立验收.json
```

先按准备脚本的固定版本、哈希和许可清单显式下载，再为新 profile 建立派生索引；不要把验收副本替换成当前运行库。原104题首次测试失败已保留，随后只能称回归题；另外23题、7份不重叠正例原件另行冻结，不以其调整词表、阈值或预期。负面具体事实仅有原件不支持标签时仍不能声称系统已经拒答。两模型文件合计约263MB，DEV双ORT进程峰值约927MB；首次懒加载显著慢于暖查询。MiniLM查询超过126正文token显式降级，长正文最多16窗，E5最多8窗；均不静默截尾。最终云端回答、语音和银行规模验收另行核验。完整证据见 `设计文档/Navren检索缺口修复与验收.md`。

### 本地查询翻译实验

`NAVREN_QUERY_TRANSLATION_ENABLED=1` 显式启用 Marian 中译英检索特征，默认关闭。只对双编码器路径、合格英文资料与中文提问适用；普通产品字段查询不加载翻译会话。原问题、术语投影和译文各自检索，译文只给 E5 及 FTS5 追加召回视图。所有词法视图共用总权重1，所有语义视图共用总权重1.25；不比较跨查询BM25或跨模型余弦。一个查询视图编码失败保留其他有效视图，并显式报告 `vector_partial`。

这一路包含本地生成式翻译模型，不能把译文当作金融事实。译文不入公共／个人数据库、原件、图谱、引文或云端提示；不会改写原请求的身份、日期、金额或准入门。`retrieval.query_translation` 仅报告政策、实际使用、固定profile与受控原因，不保存问题或译文。中文明确N月可与确定性的英文月份字面量对应，其他年份、金额、比例等数值仍严格守恒。在原数字比较之外，month-support-v1还要求新增英文完整月份具有原文数字N月或有限中文一至十二月/月份锚点，次数一致；季度、相对月份和期限/存期等时长前缀不作锚点。这不验证所有时长措辞、语义或月份与年份的绑定。新政策改变profile，需要显式离线刷新manifest，原八个模型文件不改。数值变化、未知分词、超长输入、超过生成上限、未完整结束、推理失败和超时均拒绝该特征，保留原检索路径。该保护不能识别所有语义误译，实测出现过否定方向、币种和还款含义误译，所以不能据“本地执行”宣称消除了幻觉。

模型为固定 `Xenova/opus-mt-zh-en` q8 导出，共8项资产120300600字节；源模型为 Helsinki-NLP/opus-mt-zh-en，CC-BY-4.0，需要保留署名与改动说明。导出Tokenizer的空预编译正规化表通过同版本源 `source.spm` 的固定映射适配，原资产不改写。采用有界greedy1，区别于上游beam6；输入最多128token，生成最多96token，暖调用预算2秒、单次ORT运行最多0.5秒，不含模型初始化与全局初始化锁等待。ORT中止属于协作取消，不能冒称HTTP硬超时。查询不会下载模型。真实同进程E5+Marian DEV峰值约1.225GB，不能用约120MB文件大小代替内存预算。

```sh
.venv/bin/python scripts/provision_query_translation.py --help
# 明确准备后，使用隔离验收库验证；此命令不启用当前应用服务。
.venv/bin/python scripts/verify_retrieval_shortfalls.py --suite regression --split dev --encoder dual --translation enabled --output outputs/retrieval-shortfalls/新的翻译DEV.json
```

### 完整原文窗口的问句形态软偏好

`NAVREN_ANSWER_FORM_ENABLED=1` 显式启用，默认关闭；还需要结构检索与本地重排。单问中，仅检查本轮已实际评分、通过原件校验的完整 `canonical_sentence_window`。目前识别两种保守形态：原问明确否定条件并询问可选措施，原文具有邻近的负条件与显式列举；原问有两个计数槽，实际查询视图提供同一字面单位，原文存在配对数字及该单位。未知形态保留原路径，不根据金融实体、题目编号或预期答案补规则。

它最多替代一个原 canonical 首位的软偏好，不改原排名、RRF、192 候选池或准入阈值，不让未评分的子句借用父窗口资格或模型分。规划器独立保留无偏好的具体必要覆盖，再检查完整窗口的全部原 CID 成本；预算不足时整组跳过。替代软偏好不保证原首位的可选覆盖仍然保留。原件复验与形态扫描共用重排截止时间，过期清空偏好。

诊断只保留固定形态枚举、公共组 ID 和原文偏移，标记 `retrieval_hint_only`；原问题、译文、单位和提取的数值不持久化。配对数字不证明赞成/反对、人数、总数或事件，邻近条件和列举不证明同一主体或条件关系。整窗回取用于提供完整原文，不能代替金融事实、角色与最终回答的支持校验；不写入权威图断言或修改 canonical `shape` 字段。

## 人工核对的PDF入库

可选依赖单独记录在 `local_knowledge/requirements-pdf.txt`：`pdfplumber==0.11.9`、`pdfminer.six==20251230`。不改变根依赖清单；缺失或版本不符时失败，不回落成其他解析器。

```python
from local_knowledge.pdf_ingest import extract_pdf, stage_reviewed_pdf

extracted = extract_pdf(original_pdf_bytes)
# parser_id=pdfplumber-text/1，shape=pdfplumber-pages/1。
# root先对照PDF图像核对主体、表格列、日期和条件，再登记reviewed_spans。
# 每span声明真实page、整份text中的半开start_char/end_char和精确主体/维度。
candidate = stage_reviewed_pdf(
    provider, original_pdf_bytes, reviewed_spans,
    source_id="已登记PDF官方源", source_url="已登记原件HTTPS地址", title="标题",
    reviewed_by="本地审核者", review_reference="可回查人工manifest",
    published_on=reviewed_publication_date, product_id="已核验产品ID",
    effective_from=None, effective_to=None,
)
provider.approve_version(candidate["version_id"], verification_basis=candidate["verification_basis"])
provider.publish(version_ids=[candidate["version_id"]], preserve_current=True)
```

固定逐页提取，页间仅用两个换行连接；不增加生成式标题、摘要或手工修改文字。提取返回原始bytes摘要、解析文本摘要、固定参数和页范围。最多400页、16MiB原件、2,000,000字符；缓存最多两份不可变提取结果，外部返回副本。没有可提取文字的文档失败；部分无文字页面在结果中计数，不能由本模块为其生成原文。

入库严格对照原始PDF重新执行同一解析合同，并检查每个引用完全属于声明单页。文字和定位随后受SQLite不可变版本／片段约束；回查验证原始/解析摘要、精确跨度、许可、时效和图证据，不对每个引用重复解析完整PDF。人工审核标记不是自动真实性证明；外部调用方负责实际图像与原件审核记录。发布日期不能自动当合同生效日；未知日期保持未知。`exact-text/1` 的UTF-8原始字节相等合同不放宽，不能用它接收PDF原件。

## 离线验收

```sh
python3 -m unittest discover -s tests -p test_local_knowledge.py
python3 -m unittest discover -s tests -p test_knowledge_retrieval.py
```

测试使用临时 SQLite 和明确的测试夹具，不写线上知识库，不访问外网。

## HTTP本轮证据与后台共享发布

`local_knowledge.runtime.EvidenceRuntime` 已接入 `/api/agent` 的认证、会话和 CSRF 检查之后，首个云模型调用之前。先在固定正式快照上按选中对象核对所问字段；足够时直接复用。缺口触发固定公开目录读取、本地严格 JSONP 解析、原件留存、字段准入和带图出处的临时封存快照。空快照也固定，不能在同一轮悄悄采用后来其他请求发布的内容。

本轮模型消费 `tmp-…` 或已固定 `pub-…`。共享发布作业异步执行，不阻塞本轮等待正式代次。`evidence_promotions` 只保存已准入官方公共版本 ID、状态和时间，不保存用户、账户、提问、回复或画像推论。写入时以 `BEGIN IMMEDIATE` 保证最多64个待发布作业；一个后台线程每批最多16个，启动后恢复待办。失败作业只能在可信代码重新准入、许可仍在时重排；完成作业幂等去重。

固定源读取最多一个运行时任务，同键冷请求合并；成功缓存300秒，失败缓存30秒，默认总读取预算8秒。来源许可修订使旧缓存失效；超时结果不会晚到准入。操作系统 DNS 不能可靠强制取消，已有全局读取槽限制阻塞线程数量。

`RoundProductCatalog` 逐字段消费同一 `webank-direct-disclosure`、`webank-record-json/1`、`WEBANK` 渠道和原 URL 的有效版本。另一来源即使真实有效，也不能覆盖或冒充本渠道字段。名称更新从新版本原字段回查并绑定，图的历史标签只作为历史公共别名。比较问题需要补齐缓存中已识别的其他对象；对象不明确时保留缺口，固定的中文约束会同时交给主模型和独立审核模型。

主模型、审核模型每次调用前回查本轮已读取证据；最终文字、流式结果、语音回执及后续草案沿用基座的许可、原件摘要、个人修订和用户隔离门。`public_products` 工具也绑定本轮 canonical 摘要，不能绕开 RAG。实际 TTS 音频合成仍走现有云端接口。

微众新候选按原件 `extractDate` 排序：拒绝日期倒退；同日不同原件缺少受信修订号时拒绝自动覆盖。入库序号不能代替金融时间。已撤回的新日期仍作为防倒退锚点，不恢复其可用性；已核验版本重复准入不改原审核依据、图边和撤销边界。

当前自动准入范围仅为明确的官方目录字段。一般披露、费用、风险及投资者字段最多7天，销售状态仅资料当日；销售披露仍不证明即时可买。PDF新条款、完整费用解释、份额映射、收益预测和陌生来源需要独立准入流程，不能使用模型回答当原件。

```sh
.venv/bin/python -B scripts/verify_evidence_runtime.py --mode offline --out /绝对路径/新的验收目录
# 仅明确联网时选择 --mode official 或 --mode cloud。
# cloud须用CC统一密钥加载器；只运行一个合成公共目录问题，不证明真实产品质量。
```

每次验收冻结代码和测试输入，写 `results.json` 与来源哈希；验收成功须同时检查结果中的 `passed`、失败计数、候选未变化及进程退出情况。存在 JSON 文件或执行器报告产物就绪不等于测试通过。规模上限仍为单发布5000片段、一次12对象；尚未完成银行规模负载、临时快照保留期治理和真实金融语义评测。

### 多机构资料覆盖

受控公共机构表记录明确别名、国别/地区、角色、官方原件域名和依据URL。比较意图至少绑定两个明确机构且合格候选实际覆盖它们时，以机构和文档版本轮询首片段，保留全部原候选、分数与限额。发布者别名须与HTTPS官方域名一致，拒绝用户信息、非标准/空端口和非法DNS标签；未知国别组合、缺机构、歧义或非市场候选整批回退。单机构“各位委员分别”与产品字段路径不触发。policy为`public-institution-coverage/2`，`authority=source_coverage_only`只标记来源覆盖，不能证明文档讨论主体、金融关系或事实。
