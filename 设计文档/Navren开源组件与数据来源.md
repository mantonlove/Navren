# Navren 开源组件与真实数据来源

- AG-UI（MIT）：https://github.com/ag-ui-protocol/ag-ui 。安装 `ag-ui-protocol==1.0.0`，使用官方 Python 事件模型与 SSE 编码器承载模型、工具、审核的实际开始/结束事件。金融执行权限仍由原有后端控制；不展示隐藏推理、工具参数和敏感结果。
- Agent Skills（Apache 2.0 代码 / CC BY 4.0 文档）：https://github.com/agentskills/agentskills 。采用标准 `SKILL.md` 格式及渐进加载，不复制上游技能正文。Navren 专用银行、现金流、支付、卡片、财富、画像、GLM核验技能在本仓库维护；仅白名单目录可加载。
- DashScope SDK（Apache 2.0）：https://github.com/dashscope/dashscope-sdk-python 。使用官方 `RecognitionResponse`/`RecognitionResult` 适配 ASR 结果。现有同源 WebSocket、票据与权限桥保留，不声称已将整条语音链路替换成官方 SDK。
- NetworkX（BSD）：https://github.com/networkx/networkx 。构建来源明确的多重有向个人关系图；没有部署新的外部图数据库。
- 3d-force-graph（MIT）：https://github.com/vasturiano/3d-force-graph 。复用墨枢已使用的本地构建及许可证，放在 `assets/vendor`；参考索引、三维聚焦、节点检查的交互，未修改墨枢项目。
- MediaRecorder / IndexedDB：浏览器原生能力。用户的语音消息保存在当前浏览器，银行工作区隔离；ASR录音发送百炼，应用服务器不保存录音文件。不是引入新的第三方录音平台。

## 真实产品资料

来源页面：https://www.webank.com/mobile/prod_query.html 。官方页面引用的公开数据文件为 https://www.webank.com/data/product.json 。2026-10-04实际读取到1145条披露记录，文件资料日期为2022-07-14。抓取时间不能冒充资料更新时间。数据已标记为历史资料，仅供查验，不作为最新在售推荐或真实申购依据；收益、净值及当前在售状态保持未知。没有找到可验证的最新公开报价接口，仍需要官方最新目录或授权接口。

公开目录与模拟产品相互独立。公开产品工具不可提供可执行对象ID或转入模拟申购。不存在“接入真实银行账户或交易”这类宣称。

## 个人关系图的边界

银行档案和支付/收款流水派生可重建的关系，保留来源标识与业务日期。消费记录不自动等同购物偏好。启用记忆后，模型可引用本人本轮明确陈述，保存待核实资料；本人在图谱确认或删除。未确认资料不提供给模型当作已确认事实，不改变正式风险测评及金融权限。缺失的保障档案标记为未接入，不能推断没有保险。
