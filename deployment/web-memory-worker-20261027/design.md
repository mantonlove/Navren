# Navren 双向量编码隔离方案与验收边界

## 已核实的瓶颈

- 现网 `navren-public.service` 的有效 `MemoryMax` 为 1152 MiB；2026-10-27 10:07:49 CST 因自身 cgroup OOM 被系统终止，10:08:19 自动启动。随后 cgroup 只剩约 7.45 MB。
- `LocalKnowledgeProvider.__init__` 的 `schedule`、启动时 `import_market_store(publish=True)` 的 `publish→schedule`，以及 `status` 都可能在没有用户查询时装载 E5 与 aligned 两个 ONNX 会话。
- 现网已经使用磁盘快照模型输入并关闭 ONNX CPU arena；重复这两项不构成新修复。同机隔离公开短文本探针分别测得 E5 峰值 841,879,552 B、aligned 峰值 816,930,816 B，均无 OOM；Web 与 worker 并行的整机峰值仍未验收。

## 推荐结构

1. Web 进程只负责资格/原文/权限、SQLite 向量索引核验和排序，不实例化 ONNX Runtime。`get_retrieval_encoder` 在 Web 模式返回保持现有 `profile_id`、维度和 `encode_query`/`encode_documents` 接口的代理。
2. 一个独立 UID、无网络、不接收云端模型/SMS/个人数据凭据的本机 Unix socket 管理进程接收有长度上限的编码请求。运行目录 `0710`、socket `0660`；客户端与服务端均校验对方 UID。不记录原问题或文档正文；仍需在原生 Linux 上验证账户及私有文件权限。
3. 管理进程在独立 systemd cgroup 中按 profile 启动一个编码子进程。任一时刻仅保留 **一个** ONNX 会话：E5 和 aligned 顺序执行，不同时常驻。切换 profile 时必须等待旧子进程退出并确认回收，再启动下一个；真实 cgroup 内存释放仍需联合验收。超时/OOM 返回显式失败码，Web 保留现有 FTS5 降级，不重试成多进程风暴。
4. 建索引仍按现有 `VectorIndex.build` 的 profile 顺序进行；同一 profile 的多个 `encode_documents` 批次复用其子进程，完成后关闭，再切换下一 profile。Web 的原文、权限、派生索引写入与当前流程一致，编码进程不打开业务数据库。
5. Web 的 `status` 分别报告持久化索引完整性与编码进程可用性；冷态或工作进程故障不能误报 `ready=true`。从原文到两 profile 的版本 ID、向量 manifest、RRF 排序与回退规则均需保持。

候选使用编码器固定常量生成静态 profile 指纹，并在无 ONNX 会话的构造器截断测试中核对两者完全一致。Web 查询 `profile_id` 不连接 worker；worker 每次加载真实模型都核对指纹，返回编码结果再次与 Web 静态指纹比对。`peek` 不加载模型，也不排队等待推理；它报告两个 profile 在**当前 worker 生命周期**内各自成功加载的验证记录，清洁切换保留记录，失败撤销对应记录，重启清空。冷态即使索引完整也保持 `ready=false`。一次向量检索共享 60 秒预算，忙时请求快速降级。启动后台线程先核验两份已有索引，均完整则不启动模型；新索引延后交由 worker 构建。只对推理故障、构建超时和临时 SQLite 锁针对当前 release 依次在 900/1800/3600 秒后最多重试三次，真实服务上的构建、重试及资源压力仍须验收。

## 改动与 Git 门禁

- 涉及 `local_knowledge/retrieval_encoder.py`、`vectors.py`、新增 worker/client/protocol，以及 systemd unit/socket 和权限配置；现网 `multilingual_embeddings.py`、`aligned_embeddings.py`、`onnx_memory.py` 的字节已通过 PR #6 补入 GitHub。后续以最终 GitHub main 精确 blob 为准。
- 所有生产源和配置先在 GitHub main 验证精确 blob，再分阶段部署：先启动隔离 worker 并测单模型，再切换 Web 代理。保留旧字节、旧 unit 和逐文件 CAS 回滚。

## 必须实测后才能发布的门槛

- 同机固定**公开**短文本单模型探针已分别测得上述峰值；仍需验证真正 worker 的建索引峰值、冷/热切换、退出后释放，以及 Web 与 worker 同时运行时的整机内存和时延。单模型探针不能替代联合验收。
- 公开固定题集需比对切换前后两个 profile 的 ID、384 维单位向量、检索成员/排序与已有引用核验；测试新 release 的自动建索引，确认没有长期 FTS5 退化。
- Web 重启后核对其无 ONNX 会话/快照 fd，Web 与 worker 各自 cgroup 峰值、整机 MemAvailable/Swap/PSI；在至少一次正常市场更新和代表性跨语言查询后无 OOM/超时。单次 HTTP 200 或瞬时内存下降不足以验收。

## 更小的应急选项与代价

现有隔离候选 `NAVREN_VECTOR_BACKGROUND_BUILD=0` 可阻止 Web 启动时的后台双模型加载，`status` 冷态诚实返回未就绪；它**不能**保证首次跨语言查询不 OOM，且新 release 不自动建向量索引。因此该候选单独发布不是完整修复。只启用 E5 也会失去 aligned 的跨语言召回，需业务方接受性能/质量回退后才可采用。
