# 单模型进程 worker 原型

**用途：记录 worker 的实现约束和上线验收边界。文件进入 GitHub 或服务器，不等于真实双进程稳定性已验收。**

## 范围

- `protocol.py`：私有 Unix socket，4 字节长度前缀 + JSON，每帧最多 1 MiB；接受不加载模型的 `peek`、`describe`、`query` 和最多 4 段的 `documents`。编码请求可带单调时钟截止时间，过期请求不启动模型。
- `supervisor.py`：同一时间最多一个 profile 子进程。切换前发送关闭、等待、终止/强制终止并确认回收；不能确认回收时拒绝启动下一模型。当前 supervisor 生命周期内保留两个 profile 各自成功加载的验证记录，清洁切换不误报另一个模型不可用；加载、推理、进程失败撤销对应记录，重启后重新验证。超时/崩溃返回规范错误码。
- `child.py`：子进程才导入 ONNX 编码器；模拟后端为默认值，真实后端必须显式选择。子进程不打开数据库，不读云模型/SMS 凭据。
- `profile_spec.py`：只读现有编码器的固定模型常量，计算与当前构造器完全相同的 profile 指纹、维度和窗口数；不创建模型会话。测试在阻断 ONNX 创建的位置比对两构造器原生指纹。
- `client.py`：Web 侧代理的 `profile_id` 等属性仅取静态指纹，不连接 worker；真实编码响应必须回传相同指纹。`runtime_verified()` 只做不加载模型的 socket `peek`，默认 0.3 秒超时。编码单次默认 40 秒；一次向量检索的所有视图共享 60 秒预算，超时显式降级。
- supervisor 将 socket 读取与编码操作分开处理：状态查询不排队等待模型加载/推理；忙时的新编码请求即时返回失败，由现有 FTS5 路径降级。最多 8 个 socket 处理线程，同一时刻仍只允许一个模型操作和一个模型子进程。
- `../worker-integration.patch`：更改 `retrieval_encoder.py` 的工厂路由及 `vectors.py` 的状态/后台建索引策略。冷态 `status` 对已核验索引只报告 `index_ready=true`，`ready=false`；当前 worker 已分别成功加载两 profile 后，双模型 `ready=true`；任一模型失败即降为部分就绪。真实搜索与构建仍采用 E5、aligned 双分支及原有 FTS5 回退。
- 启动后台线程先以静态 profile 完整核验已有两索引；都有效则不加载模型。新/损坏索引等待 15 秒后经独立 worker 构建，不阻塞 HTTP 启动。只对 `vector_inference_failed`、`vector_build_timeout`、临时 SQLite 锁三类故障，针对现行 release 依次在 900、1800、3600 秒后最多重试 3 次；旧 release 和永久错误不循环，失败状态供诊断。真实 systemd/SQLite 场景仍需验证进程退出和新版本构建。
- `install_worker_account.sh.prototype`、`navren-encoder-worker.service.prototype` 与 `60-vector-worker-client.conf.prototype`：专用 `navren-encoder` 系统用户和同名组、禁用网络且不传凭据的 service，以及 Web 指向 socket 的配置草案。安装脚本只给 `navren-web` 增加附属组，保留既有组。worker 使用独立的磁盘 `StateDirectory` 存放 ONNX 快照、禁用 swap；运行目录 `0710` 只允许 Web 遍历，不能创建或删除 socket；socket 为 `0660`。worker 校验连接者为 `navren-web` UID，Linux Web 客户端在发送文本前校验服务端为 `navren-encoder` UID。1024 MiB worker 上限仍须结合双进程同时运行的实测确认。
- unit 的 `WorkingDirectory` 指向现役 Web app 根；每次新 release 须将 GitHub 精确版本的整个 `worker_proto` 包复制到该根并核对导入路径、文件摘要和只读权限，或同步更新 unit 指向新根。仅合并 PR 不会更新服务器上的模块。
- `measure_public_fixture.py`：固定公开测试文本，仅读取 cgroup 内存指标、耗时、OOM 计数与整机 MemAvailable/SwapFree，不读业务 DB 或用户提问。此 worker 内脚本尚未在 ECS 执行。

## 已验证

- 16 条模拟协议/进程测试：包括同一时刻单子进程、切换回收、不可回收时拒绝新模型、socket 权限、`0710` 运行目录与拒绝 `0770`、对非授权 UID 拒绝、帧上限、超时、崩溃与恢复；并发状态查询、忙时即时拒绝、过期请求不启动模型、共享检索预算与成功加载记录的失效/恢复。
- 3 条客户端身份测试：错误服务端 UID 在收到查询文本之前被拒绝；缺失 peer 凭据或专用账户时也拒绝。
- 3 条账户安装脚本模拟检查：拒绝错误组、异常附属组和不满足前置条件的安装。
- 1 条 `VectorIndex` 模拟集成：冷态 status 无模型加载且不误报、双分支返回两组向量候选、崩溃时 FTS5 降级，Web 测试进程不导入 ONNX Runtime。
- 1 条静态 profile 与两个真实构造器指纹对照测试；10 条后台索引调度测试：已有索引不加载模型，新索引延后构建，三类临时故障有界重试、旧版本和永久错误停止。
- 35 条现有模拟向量契约通过。基线 patch 可精确还原两份集成候选文件。
- 现有 2 核 2 GiB ECS 上，另一个隔离的公开短文本单模型探针分别测得 E5 cgroup 峰值 841,879,552 B、aligned 816,930,816 B，均无 OOM；探针代码见 GitHub PR #8/#9。此结果不覆盖 Web 与 worker 同时运行或真实新索引构建。

## 发布前阻断条件

1. 现网两编码器及 `onnx_memory.py` 的基线已通过 PR #6 纳入 GitHub；最终 worker 源、集成 patch、安装脚本和 unit 仍须先进入 GitHub main，核对精确 blob 后才能更改服务器。不以正式本地仓库的脏工作树替代现网基线。
2. 单模型隔离公开探针已测得上述峰值；仍须在当前 2 核 2 GiB ECS 上验证 Web 与 worker 并行、冷/热切换、新索引构建和退出后释放，才能确定 1024 MiB worker 上限是否可用。
3. 验证 unit 的 Linux 沙箱、模型只读路径、磁盘快照写路径、`SO_PEERCRED` 和 socket 权限；worker 必须不继承 Web 的云模型、短信或个人数据密钥。
4. 固定公开材料对比两 profile 的 profile ID、向量长度/范数、旧索引完整性、双分支检索与新 release 建索引。测试真实跨语言查询、超时/崩溃 FTS5 回退。
5. 在原生 Linux/systemd 与实际公共知识库上确认 900/1800/3600 秒有界重试只针对现行 release、不会重复启动模型或导致空转；不能以永久 FTS5 代替原功能。
6. 受控切换 Web 后确认 Web 无 ONNX 模型 fd，观察 Web/worker cgroup 与整机压力，经历自然市场更新及代表性检索后仍无 OOM。单次 HTTP 200 不代表稳定。
7. 当前隔离候选已改为专用 `navren-encoder` UID/组，但尚未在服务器创建或运行真正 worker。部署前须核对模型目录可读、Web 凭据与私有目录对 worker 无读取权限、运行目录 `0710` 与 socket `0660`、双向 `SO_PEERCRED`；Web 重启后还须核对进程组列表包含 `navren-encoder` GID。账户安装脚本及 unit 精确字节必须先合并至 GitHub。只在配置文件中改 `User=` 不构成隔离验收。

## 仍有限制

串行单模型会使跨语言查询发生 profile 切换和第二次冷加载。`ready=true` 表示两个 profile 在当前 worker 生命周期内各自成功加载，不能保证之后在不同整机负载下再次切换仍会成功；失败会撤销记录并降级。worker 子进程因内存上限失败时，Web 保持服务但向量分支会降级。当前原型不是可直接上线的完整交付。
