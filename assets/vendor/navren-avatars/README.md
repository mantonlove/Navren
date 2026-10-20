# 默认头像资源

使用 DiceBear core 9.4.2 在浏览器本地生成 Navren 自有 SVG 默认头像。默认头像的种子是已绑定用户 ID；不向头像 API 发送用户资料，不替换已上传的头像。

- 头像图形：Navren 个人流线徽章。浅雾蓝底、折叠蓝色曲面与细微边缘高光，呼应产品的航迹意象。按注册用户身份固定变化弧度与同系配色；昵称变化不会改变图案。32 px 聊天头像保留清楚的轮廓。
- 使用品牌材质与色系，以浅色个人徽章区分 Navren 助手的双蓝标志，不使用人物五官。
- DiceBear core 代码：Florian Körner，MIT。完整许可随本目录保存。

曾使用的 Micah 卡通人物、海玻璃球形和墨蓝实心双流线已退出默认生成器。

重建 `../navren-avatars.min.js`：安装 `@dicebear/core@9.4.2` 与 `esbuild@0.25.11`，对本目录 `entry.mjs` 打包，使用 `bundle: true`、`format: 'iife'`、`globalName: 'NavrenDefaultAvatars'`、`minify: true`、`legalComments: 'inline'`。
