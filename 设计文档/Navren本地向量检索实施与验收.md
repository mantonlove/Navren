# Navren 本地向量检索实施与验收

日期：2026-10-10。状态：本地检索模块通过，独立双审通过；现役服务待采用。技术候选的送审快照见 outputs/local-vector/reviewed-design-snapshot.md，最终留证见 outputs/local-vector/acceptance.json。

## 用户要求与实现

本地完成公共资料索引、embedding 和混合检索，敏感个人数据不写公共库或 embedding 日志；最终交互仍由既有云端模型承担。向量负责候选召回，原文、身份、时效、权限、图路径和最终输出门继续裁决结论。

原链路是 FTS5/BM25 + typed evidence graph。现链路在同一 `LocalKnowledgeProvider.search_public` 内加入本地语义候选与加权 RRF，消费合同原四个字段保持，新增 retrieval 诊断；没有另建聊天模型链。个人库和前端不变。

选用固定 BAAI bge-small-zh-v1.5 / Xenova ONNX q8 导出、Tokenizers、ONNX Runtime CPU 和 SQLite 持久化向量。原模型 512 维，CLS 池化并 L2 归一；每窗 512 token、64 token 重叠、最多 8 窗，取片段各窗最佳分数。模型许可来源为原 BAAI 模型 MIT；ONNX 导出固定 revision `75c43b069aac4d136ba6bc1122f995fedcfd2781`，三份文件固定 SHA256。根 requirements 不修改。

技术依据：[BAAI 官方模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)、[Xenova 固定导出](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/75c43b069aac4d136ba6bc1122f995fedcfd2781)、[ONNX Runtime 官方 Python API](https://onnxruntime.ai/docs/api/python/api_summary.html)。这些来源说明模型/推理方法，不证明 Navren 金融推荐正确。

## 索引、更新与边界

- 新增 `vector_releases`、`chunk_vectors`，保存在公共 SQLite，原表和个人库不迁移。索引是派生数据，权威资料仍是原件与可核验引文。
- 建索引只读指定封存发布成员，核验公共/真实标志、当前读取/出站许可与原文 hash；候选、未发布资料、个人表不能进入索引。
- 索引与 release manifest、模型 profile、每条文本/向量 hash 和完整向量 manifest 绑定，提交使用一个写事务；失败保留旧索引。跨快照复用只取完整验证通过的旧索引。
- 已准备好模型时，启动及新发布触发单线程有界后台索引，不等待建库才能回复。临时快照也按其自身代次建索引，不能借当前正式快照。尚未就绪、模型不存在、维度/profile 变化、损坏均显式 FTS5 降级。
- 查询不下载模型、不存问题、不扫个人库、不传外网；向量候选先受身份、份额、渠道、时间、冲突、图路径核验约束，再最近邻排序，避免错误产品挤占 top-k。
- 读取后仍经 canonical resolver；撤回或许可变化后不能用旧向量继续输出失效正文。模型分数不是金融事实置信度，也不证明回答覆盖用户所问事实。

规模上限仍为每快照 5000 片段、每问最多 8 条，精确余弦全量扫描。单进程后台单线程，每批 4 条，最多 8 个排队项；300 秒检查点期限无法中断正在运行的原生推理，但越期返回后拒绝提交，排满或失败继续关键词分支，可显式重试。不声称已验证银行级并发。历史索引不自动删除，磁盘增长、多进程总负载与模型内存应在扩容前另行治理。

## 验证与已知失败

本机为 arm64、8 GiB，项目 Python 3.14。公开模型三文件合计 24,450,683 字节，不包括依赖；真实进程首次加载约 0.3—0.4 秒，完整题集测试峰值内存约 186—197 MB（十进制；复跑值以 JSON 为准），短问热查询约 5 ms。长片段两窗覆盖尾部、并发编码稳定、过长输入受控拒绝，模型查询禁用 socket 连接后仍成功；这不是操作系统级网络审计。

人工合成语料 12 段、24 问，目标标签在脚本中人工固定。6 个口语开发正例用于初校准阈值（0.40、相对最佳差 0.12、RRF 向量权重 1.25），12 个验证正例另列；每组另有 3 个日常无关问法。不能把合成正文称为官方产品条款验收。

| 题集 | FTS 前三命中 | vector 前三命中 | hybrid 前三命中 |
|---|---:|---:|---:|
| 开发正例 | 1/6 | 5/6 | 5/6 |
| 验证正例 | 11/12 | 12/12 | 12/12 |

两组日常无关问题均 0/3 返回结果。模糊问题“存进去的一万会不会变少”没有召回人工目标风险段，反而召回费用/保障背景资料，保留为失败；这说明短句和存款/理财意图歧义仍需澄清，不能自动把费用段当作本金安全结论。独立金融负例探测（如股票开盘价、最新降准日期、明年涨幅）仍可能召回主题相关背景，这属于候选层限制，不能据此回答库中不存在的事实。具体数值回答、时点和推荐适当性必须由现有证据/指标合同覆盖，不能由相似度补造。

模拟 encoder 的确定性安全测试和真实神经模型效果分开统计。已执行 181 项限定回归通过（原件/图谱/检索消费、runtime、HTTP RAG 和新增 35 项向量测试）。独立本地审查发现的中途撤回/权限取消后仍提交、末批次超过期限后仍提交，已在提交事务中重新核验并修复；故障反例加入测试。先前第一次测试因为缺少 tests 导入路径，`test_rag_integration` 未导入，已用 `PYTHONPATH=.:tests` 修正重跑，原始失败日志不作为通过证据。后续代码修改需重新执行相关项。

现役公共库只按 SQLite 只读 backup 复制到 outputs/local-vector 做另行索引验证；其 282 个片段生成 458 条窗口向量，构建约 20.8 秒。现役公共库以市场报道为主，未登记请求的微众产品 ID 时真实返回 identity_unverified，不能拿别的报道当产品条款。另一条 ECB 调查报道请求在副本上真实使用混合检索，经 consumer 原文/出处/定位回查返回 3 条，并保留部分过期、未知有效区间的缺口。未修改现役资料库、未启动/重启现役服务。真实云端最终答复、个人资料语义检索、生产发布、银行大规模吞吐均未覆盖。

## 复现与交付

```sh
.venv/bin/python -m pip install -r local_knowledge/requirements-vector.txt
.venv/bin/python scripts/provision_local_embedding.py
.venv/bin/python scripts/build_local_vectors.py --database /指定公共库.sqlite3
.venv/bin/python scripts/verify_local_vectors.py --out outputs/local-vector/semantic-evaluation.json
PYTHONPATH=.:tests NAVREN_VECTOR_ENABLED=0 .venv/bin/python -m unittest tests.test_local_knowledge tests.test_knowledge_retrieval tests.test_knowledge_resolver_performance tests.test_evidence_runtime tests.test_rag_integration tests.test_local_vector_retrieval
```

复查只发送相关源码、合成题集结果及验证摘要，不传现役公共库正文、私人库或用户聊天。正式上线时由现有服务负责人将新模块与可选依赖同步至实际运行环境，确认模型目录后重启，核对 status 中 `neural_embeddings_available` 和真实 `search_public.retrieval`。源码接入、隔离模块通过、现役服务采用须分开记录。
