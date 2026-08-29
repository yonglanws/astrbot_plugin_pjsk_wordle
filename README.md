# astrbot_plugin_pjsk_wordle

PJSK（プロジェクトセカイ / 世界计划）音乐游戏猜曲 **Wordle** 插件，本项目的玩法和界面高度借鉴自watagashi-uni的宵崎奏Bot

## 玩法

游戏开始后，bot 会从题库中随机选定一首目标歌曲，
玩家 **@机器人 + 曲名或别名** 进行回答，在 **最大猜测次数**（`max_guesses`，默认 8，可自定义）内猜出目标曲目（未 @ 机器人的消息不参与游戏）。匹配规则：
自动忽略大小写/全半角/空白/常见标点；长度 ≥4 的输入支持编辑距离容错
（漏字/多字/错字 1 个，≥8 字放宽到 2 个，如把“25时的情热”打成“25时的情熟”、
少打成“25时的情”都能命中）；只记得半个名字时，若唯一命中一首歌也可识别；
多种可能命中时择优。**保底命中**：任何 @机器人 的回答都至少匹配出
最接近的一首歌（`fuzzy_always_match` 配置，默认开启，关闭后歧义/无匹配时不作答）。
每次猜测后返回 7 个属性的反馈棋盘：

| 属性 | 类型 | 反馈规则 |
| --- | --- | --- |
| 曲名 | 字符串 | 猜中答案即获胜（绿色） |
| 上线时间 | 日期 | 精确=绿，相差 ≤180 天=橙，否则深色；带 ↑/↓ 箭头 |
| 是否为书下曲 | 布尔 | 是/否（書き下ろし游戏原创曲，精确=绿，否则深色） |
| 乐曲分类 | 枚举 | 精确=绿，否则深色 |
| 作者 | 字符串 | 精确=绿，否则深色 |
| BPM | 整数 | 精确=绿，相差 ≤10=橙，否则深色；带 ↑/↓ 箭头 |
| MASTER | 整数 | 精确=绿，相差 ≤1=橙，否则深色；带 ↑/↓ 箭头 |
| APPEND | 布尔 | 有/无，精确=绿，否则深色 |

方向箭头：`↑` 答案更晚/更高，`↓` 答案更早/更低。

**计分**：按最大猜测次数四等分，由快到慢得 4/3/2/1 分（默认 8 次时：第 1-2 次得 4 分，3-4 次得 3 分，5-6 次得 2 分，7-8 次得 1 分）。
多人可同时参与，**只有最后完整答出目标曲目的玩家**得分。

**结算消息**：
- QQ 官方机器人：结算消息以 markdown 发送，连接入口使用 QQ 官方 markdown 参数指令标签
  `<qqbot-cmd-input text="URL编码的@官机+指令" show="URL编码的显示名" />`
  （点击自动在聊天框 @官机 + 指令），并附其他 PJSK 娱乐插件的快捷入口
  （猜歌/猜曲绘/猜卡面/歌词猜曲；标签模板可用 `connect_link_template` 配置适配）；
- 普通 QQ：仅提示切换题库指令。

## 指令

| 指令 | 说明 |
| --- | --- |
| `wordle` / `Wordle` / `pjskwordle` | 开始一局 |
| `自动wordle` | 自动模式：每局结束自动开下一局（发送 `退出` 停止） |
| `切换国服题库` / `切换日服题库` | 切换题库服务器（按会话记忆） |
| `wordle排行榜` / `群wordle排行榜` | 全局 / 本群排行榜（PJSK 猜卡样式） |
| `wordle分数` | 查看我的战绩 |
| `wordle绑定 QQ号` | QQ 官方机器人账号绑定（与其他 PJSK 插件同款绑定体系） |
| `wordle帮助` | 玩法帮助图 |
| `更新wordle题库` | （管理员）强制刷新题库 |

游戏中发送 `退出` 可结束当前局。

## 资源

- 日服master：[Team-Haruki/haruki-sekai-master](https://github.com/Team-Haruki/haruki-sekai-master)
- 国服master：[Team-Haruki/haruki-sekai-sc-master](https://github.com/Team-Haruki/haruki-sekai-sc-master)
- 中文译名：`translation.exmeaning.com`（Moesekai 翻译源）
- 歌曲别名：`moe.exmeaning.com/data/music_alias`（Moesekai 别名源）
- BPM：`moe.exmeaning.com/data/music_bpm`

仅拉取上述必要 JSON 文件（含 `versions/current_version.json`），**优先走 GitHub Contents API**；题库版本号显示 `dataVersion`（如 6.8.0.12），
GitHub API 不可用（限流/断网/文件过大）时自动回退 jsDelivr CDN。
题库持久化于 `data/plugin_data/pjsk_wordle/musicdata/`，每 24 小时自动更新。

## 依赖

`Pillow`、`pilmoji`、`aiohttp`、`aiosqlite`（见 `requirements.txt`）。
图片全部使用 Pillow 本地渲染。

## 致谢

部分代码及架构参考自 [astrbot_plugin_pjsk_guess_song](https://github.com/nichinichisou0609/astrbot_plugin_pjsk_guess_song)。在此致谢