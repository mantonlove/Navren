---
name: code-sandbox
description: 用户要求生成并执行受限代码核验本轮既有金额指标时，使用隔离 WAT worker 复现已签发结果并说明实际支持范围。
metadata:
  role: advisor
  tools: [run_generated_code, account_summary, money_calculation, answer_reviewed]
  delivery: native
---

# 受限代码核验

先读取本轮正式签发指标。只有完整、无假设和覆盖缺口、可计算的整数分指标可执行；传入原 `metric_id` 和 WAT，不提交身份、路径、原始数据或权限。尚无合格指标或运行依赖时如实说明不可用。

当前支持 WebAssembly 文本格式 WAT 的单个既有指标复现。代码仅导出 `memory` 和 `run(i32输入指针,i32个数)->i64`，把输入 i64 复制到输出内存，并返回 `(输出字节数 << 32) | 输出地址`。禁止 imports、WASI、文件、网络、进程、环境变量与金融执行。Python、shell、任意新算法和新金融事实不在当前支持范围。

工具成功表示隔离执行及结果与既有指标一致，不代表交易已执行或独立验证原数据真实。最终说明必须通过 `answer_reviewed` 和原 GLM 审核；引用金额仍须使用原完整金额声明，不能把代码结果注册为新的业务指标。来源、身份或期限变化时停止，不复用旧执行结果。
