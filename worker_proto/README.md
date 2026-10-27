# 单模型进程 worker 原型

**状态：隔离本地候选；没有推送 GitHub、没有部署或运行真实 ONNX。**

## 范围

- `protocol.py`：私有 Unix socket，4 字节长度前缀 + JSON，每帧最多 1 MiB；只接受 `describe`、`query` 和最多 4 段的 `documents`。
- `supervisor.py`：同一时间最多一个 profile 子进程。切换前发送关闭、等待、终止/强制终止并确认回收；不能确认回收时拒绝启动下一模型。超时/崩溃返回规范错误码。
- `child.py`：子进程才导入 ONNX 编码器；模拟后端为默认值，真实后端必须显式选择。子进程不打开数据库，不读云模型/SMS 凭据。
- `profile_spec.py`：只读现有编码器的固定模型常量，计算与当前构造器完全相同的 profile 指纹、维度和窗口数；不创建模型会话。测试在阻断 ONNX 创建的位置比对两构造器原生指纹。
- `client.py`：Web 侧代理的 `profile_id` 等属性仅取静态指纹，不连接 worker；真实编码响应必须回传相同指纹。`runtime_verified()` 只做不加载模型的 socket `peek`。
- `../worker-integration.patch`：更改 `retrieval_encoder.py` 的工厂路由及 `vectors.py` 的状态/后台建索引策略。冷态 `status` 对已核验索引只报告 `index_ready=true`，`ready=false`，不虚报运行时可用；真实搜索与构建仍采用 E5、aligned 双分支及原有 FTS5 回退。
- 启动后台线程先以静态 profile 完整核验已有两索引；都有效则不加载模型。新/损坏索引等待 15 秒后经独立 worker 构建，不阻塞 HTTP 启动。worker 瞬时失败时，最多保留 8 个只针对当前 release 的 daemon 计时器，每 900 秒低频重试；来源、权限和模型文件错误不自动循环。真实 systemd/SQLite 场景仍需验证重试和进程退出边界。
- `navren-encoder-worker.service.prototype` 与 `60-vector-worker-client.conf.prototype`：无网络、无凭据的 service 和 Web 指向 socket 的配置草案。1024 MiB worker 上限只是失效保护候选，必须用原机单模型峰值实测确认。
- `measure_public_fixture.py`：固定公开测试文本，仅读取 cgroup 内存指标、耗时、OOM 计数与整机 MemAvailable/SwapFree，不读业务 DB 或用户提问。**未在 ECS 上执行**。

## 已验证

- 7 条模拟协议/进程测试：包括同一时刻单子进程、切换回收、不可回收时拒绝新模型、socket 权限、帧上限、超时、崩溃与恢复。
- 1 条 `VectorIndex` 模拟集成：冷态 status 无模型加载且不误报、双分支返回两组向量候选、崩溃时 FTS5 降级，Web 测试进程不导入 ONNX Runtime。
- 1 条静态 profile 与两个真实构造器指纹对照测试；3 条后台索引调度测试：已有索引不加载模型，新索引才延后构建，瞬时 worker 故障后可低频恢复。
- 35 条现有模拟向量契约通过。编译和基线 patch 适配检查通过。

## 发布前阻断条件

1. 将现网两编码器及 `onnx_memory.py` 的实际字节先纳入 GitHub，随后提交 worker 源、集成 patch 和 unit，核对 main 的精确 blob；不以当前正式本地仓库的脏工作树替代现网基线。
2. 在当前 2 核 2 GiB ECS 的隔离 worker cgroup 上，**分别**测单 E5、单 aligned 的冷/热时延、查询/文档批次内存峰值和退出后释放；不可在现有 Web cgroup 内做压测。测得的峰值决定最终 worker 上限，不能采用草案值直接发布。
3. 验证 unit 的 Linux 沙箱、模型只读路径、磁盘快照写路径、`SO_PEERCRED` 和 socket 权限；worker 必须不继承 Web 的云模型、短信或个人数据密钥。
4. 固定公开材料对比两 profile 的 profile ID、向量长度/范数、旧索引完整性、双分支检索与新 release 建索引。测试真实跨语言查询、超时/崩溃 FTS5 回退。
5. 在原生 Linux/systemd 与实际公共知识库上确认 900 秒重试只针对现行 release、不会重复启动模型或导致空转；不能以永久 FTS5 代替原功能。
6. 受控切换 Web 后确认 Web 无 ONNX 模型 fd，观察 Web/worker cgroup 与整机压力，经历自然市场更新及代表性检索后仍无 OOM。单次 HTTP 200 不代表稳定。

## 仍有限制

串行单模型会使跨语言查询发生 profile 切换和第二次冷加载，实际延迟目前未知。worker 子进程因内存上限失败时，Web 保持服务但向量分支会降级。当前原型不是可直接上线的完整交付。
