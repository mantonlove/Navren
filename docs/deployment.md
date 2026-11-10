# 部署与冒烟检查

本文记录 Navren 对外实例（navren.cn）的发布形态、发布步骤和最低验收。只写通用流程，具体主机、密钥与账号信息不进入仓库。

## 发布形态

```
/opt/navren/releases/<release-id>/app      # 代码树，由 git archive 解出
/etc/navren-public/runtime.json            # 指向 release，决定参数与环境变量
/etc/navren-public/run.py                  # 读取凭据、chdir、exec server.py
/etc/nginx/sites-enabled/navren-public     # 静态资源 root 必须与 release 同步
/var/lib/navren-public/state/              # 四个 sqlite（账号/银行/私库/知识库），跨版本保留
/var/lib/navren-market/public/market.sqlite3
```

- 进程入口是 `server.py`，经 `scripts/serve_financial_frontend.py` 的 `prepare()` 校验后启动；端口参数在 `runtime.json` 的 `arguments` 里。
- nginx 的 `location ~ ^/(?:js|css|assets)/` 用 `root` 直接指到 release 目录，**换 release 必须同步改 nginx 并 reload**，否则页面还是旧静态文件。
- 状态库启动时会自动迁移 schema；迁移是单向的，回滚代码前必须同时还原迁移前的库备份。

## 不在 Git 里的运行时资产

| 资产 | 为什么不能缺 |
| --- | --- |
| `outputs/financial-mcp/venv` | `prepare()` 校验的隔离 MCP SDK，缺了报 `ISOLATED_MCP_SDK_UNAVAILABLE` |
| `outputs/contest-finances/` | 四位体验角色的来源文件，缺了报 `DEMO_SOURCE_MISSING` |
| `dependency-resources/*/tokenizer.json` | token 预算校验按 sha256 读取，缺了报 `RequestBudgetError`；用 `python3 scripts/fetch_dependency_resources.py --fetch` 获取 |

## 发布步骤

1. 确认要发布的提交已在 GitHub 上，且 CI 通过。
2. `curl -fsSL https://codeload.github.com/<owner>/<repo>/tar.gz/<commit>` 取归档，校验 sha256。
3. 解到新目录，删掉 `tests/`、`设计文档/`、`deployments/`、`evaluation/`、`docs/`、`.github/`。
4. 从旧 release 复制上表资产；`python3 scripts/fetch_dependency_resources.py --check` 必须 3/3 通过。
5. `python3 -m compileall -q` 目标目录，确认没有语法错误（macOS 打包时注意删掉 `._*`）。
6. 备份状态库与 `runtime.json`，改 `runtime.json` 的 `release_path`，同步 nginx `root`，`nginx -t && systemctl reload nginx`。
7. 重启服务，执行第 8 节冒烟检查；记录 release id、提交号、归档 sha256 与检查结果。

## 冒烟检查

```sh
python3 scripts/smoke_test_public.py                      # 默认 https://navren.cn
python3 scripts/smoke_test_public.py --base-url http://127.0.0.1:60291
python3 scripts/smoke_test_public.py --json               # 机器可读
```

只发匿名 GET，不登录、不写数据。检查首页、首页引用的静态资源、`/api/welcome/auth/session`、`/api/public-market/v1/hotspots`，以及未登录访问 `/chat.html` 是否 303 跳转。任一失败退出 1，可直接接进发布脚本或巡检。

它覆盖的是“部署是否可用”，不覆盖登录后的对话、银行、研究链路——那部分需要独立验收账号。

## 回滚

```sh
systemctl stop navren-public
cp <备份>/runtime.json /etc/navren-public/runtime.json
cp <备份>/ordinary-bank.sqlite3 /var/lib/navren-public/state/ordinary-bank.sqlite3   # 关键
# 还原 nginx root
systemctl start navren-public
```

只回退代码不回退库会报“数据库来自更高版本，禁止降级打开”，这是有意的保护。
