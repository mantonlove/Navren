# dependency-resources

本目录存放离线运行需要的第三方 tokenizer 资产。体积较大的 `tokenizer.json`
于 2026-11-09 起从 Git 索引移除，改为「按清单获取 + sha256 校验」，仓库里只保留
清单、许可与说明。

## 哪些文件在仓库里，哪些需要获取

仍在 Git 中跟踪（小文件，克隆即得）：

- `deepseek-v4/LICENSE`、`deepseek-v4/provenance.json`
- `glm53-flash/LICENSE`、`glm53-flash/tokenizer_config.json`、`glm53-flash/chat_template.jinja`

已从索引移除、需要按清单获取（工作区里可能已有，但不再随 Git 分发）：

| 本地路径 | 字节数 | sha256 |
| --- | ---: | --- |
| `dependency-resources/deepseek-v4/tokenizer.json` | 6,367,146 | `8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf` |
| `dependency-resources/deepseek-v41-1017/tokenizer.json` | 6,367,257 | `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b` |
| `dependency-resources/glm53-flash/tokenizer.json` | 20,217,442 | `19e773648cb4e65de8660ea6365e10acca112d42a854923df93db4a6f333a82d` |

## 来源

上游均取自 Hugging Face，URL 固定到具体 revision，便于复现：

| 本地路径 | 上游 | 固定 revision |
| --- | --- | --- |
| `deepseek-v4/tokenizer.json` | `deepseek-ai/DeepSeek-V4-Flash` | `60d8d70770c6776ff598c94bb586a859a38244f1` |
| `deepseek-v41-1017/tokenizer.json` | `deepseek-ai/DeepSeek-V4.1-Flash-1017` | `1bae0e1088993b9b82de08f59de5be5a34e42170` |
| `glm53-flash/tokenizer.json` | `zai-org/GLM-5.3-Flash` | `46a050bec13002ad29ee795c16e61cf4fa3c4a9c` |

两点如实说明：

- GLM 资产在 Hugging Face 的组织名是 `zai-org`（智谱的官方组织）。用 `ZhipuAI`
  作为 author 查询 Hugging Face 已无结果，仓库路径请以 `zai-org/GLM-5.3-Flash`
  为准；这与运行时代码 `agent_runtime/request_budget.py` 中的
  `GLM53_REPOSITORY = 'zai-org/GLM-5.3-Flash'`、`GLM53_REVISION` 一致。
- 三个上游仓库均为公开、非 gated，许可为 MIT（各自的 `LICENSE` 已保留在本地目录）。

## 获取与校验

```bash
# 只校验（不联网）：文件存在则核 sha256，缺失则打印获取方式并以非零退出
python3 scripts/fetch_dependency_resources.py --check

# 缺失或哈希不符时按清单下载，校验通过后原子替换
python3 scripts/fetch_dependency_resources.py --fetch
```

脚本内置的清单包含「路径 + 上游 URL + 字节数 + sha256」；`--fetch` 遇到清单里
没有可靠 URL 的条目会直接报错退出（退出码 2），不会静默跳过。

核验记录：上表 sha256 由本地文件实际执行 `shasum -a 256` 得到，并于 2026-11-09
按上表 URL 重新下载副本比对，三个文件的哈希均与本地一致。清单里的字节数同样是
本地实际大小。

## 为什么必须先跑这一步

运行时代码会按固定 sha256 读取这些文件：

- `agent_runtime/request_budget.py` 的 `TOKENIZER_SHA`
  （`8f9f37ca…`）用于 `deepseek-v4/tokenizer.json`；
- 同文件的 `OFFICIAL_TOKENIZER_SHA`（`c90dfa01…`）用于
  `deepseek-v41-1017/tokenizer.json`；
- 同文件的 `GLM53_ASSET_FILES['tokenizer.json']`（`19e77364…`）用于
  `glm53-flash/tokenizer.json`。

文件缺失或内容漂移时，相关调用会 fail-closed 抛
`request_budget_dependency_missing_or_redirected` 或 `request_budget_dependency_sha_mismatch`。
因此新克隆的仓库在依赖 tokenizer 的功能（预算估算、GLM 审核等）之前，先执行
`--fetch` 或手工按上表放置文件并核对 sha256。

## 常见问题

- `--check` 报哈希不符：不要手改文件凑哈希，直接用 `--fetch` 重新下载；若上游
  文件本身变了，需先确认是否应更新清单里的 URL、revision 和 sha256。
- 不要用 `git checkout -- dependency-resources/.../tokenizer.json` 恢复：这些路径
  已不在 Git 索引里，该命令无法还原。
- 离线环境：从可信来源拷贝对应文件，再用 `--check` 核对 sha256。
- 历史清理边界：本次只用 `git rm --cached` 把大文件移出索引，工作区文件保留；
  历史提交里仍包含这些 blob。用 `git filter-branch` / `filter-repo` 改写历史的
  操作没有执行，需要时由仓库所有者单独决策。
