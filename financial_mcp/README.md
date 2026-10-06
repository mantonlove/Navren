# 独立财务 MCP

官方 `mcp==2.3.0` SDK，提供11个只读工具。模型没有用户编号、数据库路径或 SQL 参数。每个 stdio 子进程由可信宿主绑定一个主体；宿主必须根据已登录会话选择主体，不能采用用户消息中的编号。此包不提供转账、申赎或保单执行。

安装到隔离环境：

```sh
python3 -m venv outputs/financial-mcp/venv
outputs/financial-mcp/venv/bin/python -m pip install -r financial_mcp/requirements.txt
```

启动配置由宿主环境提供：

```json
{
  "command": "/项目目录/outputs/financial-mcp/venv/bin/python",
  "args": ["-m", "financial_mcp.server"],
  "cwd": "/项目目录",
  "env": {
    "NAVREN_FINANCIAL_DB": "/数据目录/financial_data.sqlite3",
    "NAVREN_FINANCIAL_SUBJECT_ID": "tycoon"
  }
}
```

四人 subject 为 `tycoon`、`grain_farmer`、`working_landlady`、`retired_entrepreneur`。真实宿主按当前会话映射，不让模型切换主体。金额单位为人民币分；个人数据来源为 `navren_synthetic`，不代表真实银行客户或真实产品收益。

依赖服务接口：`financial_data_service.service.FinancialDataService(database_path).query(subject_id, operation, arguments)`。读取合同见 `contract.py` 与设计方案。DB 必须预先存在，缺配置或服务失败时停止启动，不自动创建空库掩盖缺数据。

主应用没有 MCP SDK 时，可用隔离环境执行 `python -m financial_mcp.bridge`。stdin 为一个 JSON 对象：`{"action":"call_tool","name":"financial_list_accounts","arguments":{}}`；stdout 为单个 `{"ok":true,"data":...}` 响应。桥接器内部使用官方客户端执行 initialize 和 tools/call，不是直接调用 Python 服务冒充 MCP。主体和路径仍只能由宿主环境传入。此桥接每次建连接，适合首版；高频调用可以在消费端按授权主体复用会话，不能混用不同主体连接。

迁移到 Linux：重新建虚拟环境、安装固定依赖，配置服务器上的数据路径；Navren 服务通过 stdio 启动本机子进程即可继续使用，无需暴露网络端口。需要跨主机 HTTP 时再实现授权与传输安全，不能直接把本机 stdio 的信任假设搬到公网。
