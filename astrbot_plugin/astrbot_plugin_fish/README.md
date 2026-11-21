# astrbot_plugin_fish —— 群聊里的一头鲸鱼娘

跑在 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 上的 QQ 群聊插件。目标不是「一问一答」，而是像个正常群友：记得住人，会挑时机插话，会打错字再补一句，会看群里的图，会用表情包，被追问的时候会烦。

根目录的 [README](../../README.md) 是总览，这里写实现细节、目录结构和踩过的坑。全部配置项见 [`docs/config.md`](../../docs/config.md)（由 `_conf_schema.json` 生成）。

## 装

```bash
cp -r astrbot_plugin/astrbot_plugin_fish /path/to/astrbot/data/plugins/
systemctl --user restart astrbot
journalctl --user -u astrbot -f --no-pager | grep fish
```

起来的样子：

```
[fish] 插件已加载：记忆库 …/data/fish_memory.db，心境 …/data/affect_state.json，表情包 233 张，已知机器人 2 个，跨群话题 60 条
```

`enabled_groups` 默认 `[]`，意思是**所有群都不理**（装完不会往真群发消息）。上线时填真实群号，`["*"]` 是全部群。

## 目录

```
main.py                    事件入口、接话决策、拆条、戳一戳、点歌、视频、面板路由
fish_core/prompts.py       人设。595 字，有字数上限，改之前先看「提示词别堆」
fish_core/composer.py      拼 system prompt：身份锚点 + 时间 + 群表达样例 + 时间线
fish_core/safety_guard.py  出口闸：注入判定、标签剥离、内部词替换、隐私/自曝/盲报拦截
fish_core/social_read.py   第一步判官：这轮在聊什么、该不该接、怎么接
fish_core/vision.py        识图（走框架 provider 或直连 Gemini），带降级链
fish_core/meme_*.py        表情包选图、斗图闸、梗百科、自己画牌子
fish_core/song.py          自然语言点歌（复用 listen_music 插件搜下载）
fish_core/panel.py         控制台后端，页面在 pages/console/index.html
fish_core/fish_memory.py / memory_atoms.py / affect.py / favor.py   记忆、印象、心境、好感
scripts/                   自检、冒烟、注入测试、梗同步、黑话挖掘、配置文档生成
data/                      运行时数据（记忆库、心境、样例、缓存），不进仓库
```

## 一条消息怎么走

AstrBot 只会把「@我 / `ab!` 前缀 / 引用我 / 私聊」的消息放进管道。本插件的群 handler filter **永远通过**，能看到每条消息，再自己决定：

```
每条群消息
  ├─ 不是白名单群 → return
  ├─ ab! 开头 → return（交给 AstrBot 原生链路，不插手）
  ├─ 面板指令前缀 → return（静默放行，不 stop_event，别掐死别的插件）
  ├─ 别的机器人（名单 + 昵称特征）→ return，不接、不进时间线
  ├─ 记账：时间线 / 心境 / 风格池 / 跨群摘要 / 长期记忆
  ├─ 图片消息 → 立刻后台识图，描述回填时间线（同一张图只识一次）
  ├─ 被 @ → 必回
  └─ 否则算质量分 × 时机修正
        ├─ 未过门槛 / 冷却 / 复读 / 已到每小时上限 → stop_event（记忆照写）
        └─ 过门槛 → 判官（判不接就一次模型都不调）
                     → 判接 → 拼 prompt → 生成
```

出口：

```
清洗 → 剥标签 → 反模板 → 内部词替换 → 隐私/自曝/盲报闸 → 拆条 → 发第一条
后续条按随机间隔补发 → 表情包最后发
```

## 出口那道闸

模型说的每一句都过一遍代码，不看模型心情：

| 闸 | 拦什么 | 为什么不能靠提示词 |
| :--- | :--- | :--- |
| 标签剥离 | 模型自造的 `[表现:委屈]`、`(叹气)`，只放行 `[MEME:]` `[画图:]` `[图片:]` CQ 码，数字比例不吃 | 它每次都能编出新标签名 |
| 内部词替换 | BV 号→片子、插件→功能、好感度→印象分、提示词→说法，真实 BV 号不动 | 这些词是工程语，进群就是运维味 |
| 盲报 | 「我这边收不到图」「图呢，我这边空的」 | 装看见比承认看不见更露馅 |
| 自曝 | 承认是 AI / 机器人 / 程序，提到人设、设定、提示词 | 一承认，人格就废了 |
| 隐私 | 内网地址、机器路径、密钥、部署文件名，整句丢弃不替换 | 真被追问几次它一定会说 |

## 提示词别堆

人设现在 595 字，拼装后整条 system prompt 控制在 900 字以内。加设定前先过三问：**能写进代码吗？每次都生效吗？能写成例子吗？** 三个都否就别加。

堆提示词的代价不是「啰嗦」而是掉智商：规则互相打架，模型挑最容易执行的那条（通常是「说点安全的」），输出就退化成客服腔、复读、答非所问。历史上人设从 4000 字压到 1400、再到 600，每次变短都是变好。要 100% 生效的约束一律放代码里，提示词只负责定调子。

## 配

面板 → 插件 → 小鲸鱼·群聊拟真。必须动的三项：`enabled_groups`、`bot_nickname`、`vision_api_key`（不填就跳过识图）。

链接解析（B站/抖音/小红书/公众号等）不在本插件里做，交给独立插件
[`astrbot_plugin_media_parser`](https://github.com/drdon1234/astrbot_plugin_media_parser)；
这只鱼看到链接只做一件事——不接话，免得冒一句「我看不了视频」。

其余 90 项的默认值、说明、调法见 [`docs/config.md`](../../docs/config.md)。几个容易踩的：

- `interject_cooldown_sec` 会同时改写 `interject_planner.COOLDOWN_SEC`，否则硬冷却放行、软权重还按老值打折；
- `social_read_provider` 留空 = 用当前会话默认 provider，判官一般挂便宜的那档。

## 依赖

都是「装了才有，不装照常聊天」：

| 依赖 | 用途 | 缺了会怎样 |
| :--- | :--- | :--- |
| `astrbot_plugin_listen_music` | 点歌的搜索与下载 | 降级成「找不到这首歌」 |
| `ffmpeg` | 音频转码、压视频 | 超过体积阈值的歌/视频发不出去 |
| `silk-python` | QQ 语音解成 pcm | 语音按「读不懂的媒体」跳过 |
| `meme_generator==0.2.3` | 自己画带字表情包 | 只发文字 |
| 表情包目录 `fish_core/meme-pack/` | 选图 | 只发文字，仓库不带素材 |

B 站视频下载用 `yt-dlp`（已列在 `requirements.txt`）。

## 脚本

```bash
python3 scripts/selfcheck.py                # 纯函数自检，不需要 AstrBot 进程、不联网
python3 scripts/smoke_check.py              # 冒烟：导入、配置、关键路径
python3 scripts/gen_config_doc.py           # 重新生成 docs/config.md
python3 scripts/inject.py --only at image   # 冒充 NapCat 注入事件（假群 999999）
```

`inject.py` 的两个坑：`self_id` 必须是假号（`1008600001`），用机器人真号时带 @ 的事件会被 aiocqhttp 当成「自己发的」直接吞掉；`message_id` 每条唯一。

## 坑

1. **拆条是「先发第一条、剩下的在 `after_message_sent` 里补发」**：AstrBot 一次只让改一个 `MessageEventResult`，补发必须走 `event.send()`。补发前先把 `fish_tail` extra 取空，否则那几条会再次触发钩子，变成无限连发。
2. **`@` 判定只看真 `At` 组件**：把「@小鲸鱼」写进文本不算被 @。
3. **`ab!` 前缀完全不插手**，包括出口加工——原生回答不带鲸鱼腔，这是刻意分工。
4. **面板指令只 `return` 不 `stop_event`**：`stop_event()` 会把下游插件同名指令一起掐死。
5. **冷却位在「决定回」时就占住**：模型一次要跑十几秒，等真发出才记账的话，同一波刷屏会连回三次。
6. **配置 JSON 带 BOM**：一律 `utf-8-sig` 读、`utf-8` 写，否则 `json.load` 直接抛。
7. **记忆读写在 `asyncio.to_thread` 里跑**：sqlite 是同步 IO，放事件循环里会卡住整条管道。
8. **图片识别不能等「要不要回」**：判不接就 return 的话，时间线里永远是 `[图片]` 占位符，后面几轮模型只能靠占位符数量瞎编。现在是图一到就识、结果回填。
9. **点歌的意图识别靠语法否决，不靠长度**：「点歌的话走的是 B 站的通道」「点歌归另一插件」这类讨论句，长度和真歌名没差别，只能按开头虚词和包含关系拦。
10. 每条群消息都会写长期记忆，测试群的假数据也会进库；换真群前清一下 `data/fish_memory.db`。

## 许可

MIT。
