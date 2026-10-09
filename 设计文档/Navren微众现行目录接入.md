# Navren 微众现行目录接入

> 历史设计或实施记录：保留文内注明版本的测试结论，不能作为当前版本的验收结果。当前实现与运行方式见 [README](../README.md) 和 [技术说明](../TECHNICAL.md)。本机机器信息已清理，未发布的历史产物不提供仓库链接。

接入与验收日期：2026-10-05。已在 `项目根目录` 切换实际产品目录入口并更新本地预览服务。

## 数据来源与范围

现行微众银行[产品信息查询](https://www.webank.com/inquiryCenter/query)使用的[公开目录](https://tctp.tctpwebankcdn.net/owb-admin/data/appsystems/owb-admin/productpresentation.json)已接入。资料日期为 **2026-09-24**，这次请求取得 **13,658 条不同产品代码**；文件散列为 `dab1216c8bf2911dc5c90542b1d57423fd86d238a290d385e5c7720155a2f0ed`。

| 字段/类别 | 结果 |
|---|---|
| 快照销售状态 | 在售 11,727；存续 1,930；停售 1 |
| 产品类型 | 开放式基金 11,928；理财产品 1,572；信托计划 83；保险 32；集合资产管理计划 24；其他 19 |
| 客群字段 | 81 条为空，保留未知 |
| 原文 | 各产品链接到微众现行官方详情页 |
| 费用 | 保留官方文字说明；没有补造具体渠道费率 |

获取时间与官方资料日期分别保留。9 月 24 日的销售状态是带日期的披露，当前可申购资格、渠道额度、客户持仓和实时费率仍未知；`current_sale_status=null`、`annual_return=null`、`executable=false`。原有 3 份东方基金管理人净值资料继续保留，来源、日期和版本独立。

## 实现

- `webank_catalog.py` 读取固定 JSONP 包装并按 JSON 解析，校验大小、来源日期、字段类型和产品代码唯一性；不执行下载内容。
- `public_products.py` 是兼容入口，现行应用与 Agent 的目录查询都使用新适配器。
- 目录按需获取，每小时可重新核验一次；一小时内复用内存快照。失败保留上一次核验内容与原日期，不回退旧 2022 目录或模拟数据；冷启动失败显示不可用。缓存会随日期变旧。
- `/api/finance/public-products` 每页最多 500 条，发现工作区核对数量、版本、分页位置及结束标志，完整读取后才发布目录，防止部分数据伪装完整。
- 产品筛选覆盖完整目录，首屏渲染 100 条，可加载更多。风险筛选识别 `R1～R5`，详情保留官方原文；无明确等级的文字列入“未披露 R 等级”。
- 公共目录、管理人净值与模拟业务产品继续分别标注身份，公开资料没有申购按钮，跨页引用与隐私重置保留版本校验。

## 验收

1. 8 项目录测试通过，包括真实完整快照、异常输入、失败缓存、跨日期新鲜度和实际 HTTP 路由的 500 条分页及末尾记录。
2. 既有个人生命周期与页面来源合同回归通过；发现工作区既有回归通过。
3. 新增前端 10 个场景通过，覆盖 13,658 条全量分页、100 条分批展示、尾部搜索、风险文字、日期销售状态、分页异常与隐私晚到。
4. 实际本地预览接口读完 **28 页**，末页 **158 条**；数量与唯一代码均为 **13,658**，3 个原有基金代码和目录末尾代码搜索均命中；所有公开产品均不可执行，静态脚本与当前本地文件一致。

本地版本：`navren-ded1904de7bd`。[发现页面](http://127.0.0.1:18767/recommend.html)。运行证据：`outputs/current-catalog-integration/runtime-verification.json`；可复验脚本：`outputs/current-catalog-integration/verify_runtime.py`。

这次验收范围是本地接口与内存 DOM，未进行真实浏览器实屏排版验收，未部署到阿里云公网服务器。

## 复验命令

```sh
PYTHONPATH=tests:. .venv/bin/python -m unittest test_current_catalog test_personal_lifecycle test_site_context -q
NODE_PATH=/tmp/navren-market-ui-test-20261005/node_modules node tests/test_discover_workspace.js
NODE_PATH=/tmp/navren-market-ui-test-20261005/node_modules NAVREN_CATALOG_UI_PATH=js/discover_workspace.js node tests/test_current_catalog_ui.js
```

临时 `NODE_PATH` 使用本机已有 jsdom 依赖；换机器后应提供 jsdom 的安装路径。
