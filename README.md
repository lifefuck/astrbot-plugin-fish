# QQ 群聊机器人 · AstrBot 插件

[![ci](https://github.com/lifefuck/astrbot-plugin-fish/actions/workflows/ci.yml/badge.svg)](https://github.com/lifefuck/astrbot-plugin-fish/actions/workflows/ci.yml)

中文 | [English](README.en.md)

两个跑在 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 上的 QQ 插件。重点不是「一问一答」，而是尽量像个正常群友：记得住人，会挑时机插话，会打错字再补一句，会看群里发的图，会用表情包。

协议端用 NapCat（或其他 OneBot 11 实现），模型接任意 OpenAI 兼容接口。

```
astrbot_plugin_fish/        聊天本体：人设、记忆、接话、拆条、表情包、识图
astrbot_plugin_memory_cmd/  /memary 记忆查询，独立插件，只读
```

最早那版是自研框架（`bot.py` + 自己的事件循环 + 面板），已经停用，代码存在 [qq-deepseek-realistic-bot](https://github.com/lifefuck/qq-deepseek-realistic-bot) 当存档。这里只放插件。

## 它做什么

- **人设**：一份固定人物设定 + 当前时间 + 群表达样例拼成 system prompt。
- **长期记忆**：每条群消息过一遍抽取（事实、偏好、计划、事件），按群和群友分开存；回话前只捞最相关的两条，不灌满上下文。
- **印象原子**：从记忆里再压一层短句（谁爱喝什么、谁在干什么），带衰减和去重，重复的事不会被反复提。
- **接话时机**：先按「像不像在问人 / 有没有空档 / 是不是重复」打分，过门槛才动手；再加一道社会判断，判不接就一次模型都不调。
- **免 @ 追问**：@ 它一次，之后 5 分钟内（最多再追问 2 轮）它继续把这句对话当自己的事，不用每次 @。
- **反模板**：起手撞车、结尾撞车、连续重复语气词、复述式开场，出口统一削掉。
- **手滑**：偶尔打错字，小概率再补一条纠正。
- **拆条**：超过 26 字拆成最多 3 条（心境差时只发两条），间隔随机补发；不切链接、英文长词和 ZWJ emoji，条数超了按名额均分而不是拼成一堵长墙。
- **表情包**：按情绪标签选图，一回合最多一张；刚发过、对方刚发图都拦。
- **斗图**：@ 它并甩图，它会挑一张库存反杀，先嘴一句再发图。
- **看图**：群友发图 → 缩图 → Gemini 白描 → 描述塞进 prompt，失败静默降级。
- **链接解析**：消息里带 B 站 / GitHub / 网易云 / 抖音 / 小红书 / 快手 / 普通网页，抓标题和摘要塞进上下文，不靠它瞎猜。
- **联网扩展**：被叫到又问到点子上（「这图什么出处」「搜一下」「有什么新番」）才联网，普通闲聊零外联。
- **热梗与本群黑话**：三层注入——常驻梗、这句沾边的梗、本群自己天天说的词（从真实聊天里挖出来的，详见下文「梗从哪来」）。
- **关系网**：记最近谁爱 @ 谁，回话前给一句体感，不当统计表用。
- **跨群**：别的群聊了什么留一句短摘要，必要时可以顺口提一句。
- **文字游戏**：数字炸弹、掷骰子、抽签、点名，纯本地状态机，不花 token。
- **点歌**：群里直接说「我想听晴天」，自己搜 B 站、下载、原文件发语音，等待期间先给一句进度。见下文「点歌依赖」。
- **梗释义**：有人说「XX 是什么梗」，查梗百科塞进上下文，没收录就查一次并记下来。
- **秩序上报**：白名单群里的广告拉人、表情包刷屏、严重辱骂，群里给一句提示，同时私聊上报主人（判断在提示词里，代码只做标记剥离）。
- **戳一戳**：被戳先回戳，再基于「戳之前那条没被回的消息」接一句。
- **护栏**：明确在套人设、要系统提示词、改写指令的，直接怼回去不调模型；正常提问一律放行。

## 装

前置：AstrBot 4.x 跑着，NapCat 已连上 AstrBot 的反向 WebSocket。

```bash
git clone https://github.com/lifefuck/astrbot-plugin-fish.git
cd astrbot-plugin-fish

cp -r astrbot_plugin/astrbot_plugin_fish        /path/to/astrbot/data/plugins/
cp -r astrbot_plugin/astrbot_plugin_memory_cmd  /path/to/astrbot/data/plugins/

systemctl --user restart astrbot
journalctl --user -u astrbot -f --no-pager | grep fish
```

日志里有这行就说明起来了：

```
[fish] 插件已加载：记忆库 …，心境 …，表情包 N 张
```

## 配

AstrBot 面板 → 插件 → 小鲸鱼·群聊拟真。必须动的几个：

| 配置 | 说明 |
| :--- | :--- |
| `enabled_groups` | 生效群号列表。**默认空 = 所有群都不理**，一定要填，或填 `["*"]` 全开 |
| `bot_nickname` | 群里 @ 它时用的称呼，和 QQ 昵称保持一致 |
| `interject_desire` | 主动接话意愿。0.3 左右比较自然，0.6 是群里最吵的那个 |
| `vision_api_key` | 要识图就填 Gemini key，不填自动跳过识图 |
| `vision_proxy` | 访问 Google 的代理，国内必须填 |
| `poke_back` | 被戳要不要回戳 |
| `enable_games` | 文字游戏（数字炸弹、骰子、抽签、点名） |
| `meme_battle_enabled` | @ 它带图时要不要斗图反杀 |
| `media_parse_enabled` | 解析消息里的链接和视频号 |
| `connectors_enabled` | 被叫到且明确问到时才联网（搜番 / 网页检索 / B 站热门） |
| `meme_pool_size` / `meme_rotation_sec` | 热梗池每轮注入多少条、多久换一轮 |

模型（provider）、唤醒前缀 `/` 属于 AstrBot 全局设置，不在插件里配。

还有几个容易漏的：

| 配置 | 说明 |
| :--- | :--- |
| `enabled_private_users` | 私聊白名单。**留空 = 只有主人能私聊**，填了才对外 |
| `split_max_chars` / `split_max_parts` | 拆条阈值（默认 26 字）和条数上限（默认 3 条） |
| `reply_max_chars` | 单条回复字数上限，防跑题式的长篇大论 |
| `tech_repeat_window_sec` | 技术话题复读窗口：同一串参数/型号在这个时间内说过了就不再重复 |
| `natural_song` | 自然语言点歌开关，关掉就只能走指令 |
| `tattle_watch_groups` | 主动上报的群白名单，**留空 = 一个群都不上报** |
| `order_guard_enabled` / `order_guard_cooldown_sec` | 秩序巡检总闸与同群上报冷却 |

表情包是个可选依赖：把表情包目录放在 `astrbot_plugin_fish/fish_core/meme-pack/`，索引会自动载入；没有就只发文字。仓库不带素材。

## 点歌依赖

点歌不自己实现搜索和下载，而是复用另一个插件的能力：

- 装 [astrbot_plugin_listen_music](https://github.com/57Darling02/astrbot_plugin_listen_music)（「我想听歌！」），它提供 B 站搜索、音频下载和音频处理；
- 系统里有 `ffmpeg`，超过体积阈值时才用它压一版；
- 两个插件装在同一个 AstrBot 的 `data/plugins/` 下，目录名保持 `astrbot_plugin_listen_music`。

缺任何一个，点歌会自动降级成「找不到这首歌」，其它功能不受影响。

## 梗从哪来

两层，缺一层都会「接不住梗」。

**一层是通用梗库**（`data/hot_words.json`），由同步脚本拉热搜 + 手写常青梗组成：

```bash
python3 scripts/sync_hot_words.py --dry-run   # 只看会拉到什么
python3 scripts/sync_hot_words.py             # 写盘
```

热搜榜里一大半是新闻标题（「某队夺冠」「某会为何重要」），群里没人拿它当梗接。同步脚本会把这类标成 `news` 单独归类，**不进轮换池**——不然真梗会被新闻挤掉，注入出去的东西又长又没法接。

**另一层是本群黑话**（`data/group_slang.json`），从自己的聊天流水里挖：

```bash
python3 scripts/mine_group_slang.py            # 先看挖到什么
python3 scripts/mine_group_slang.py --apply    # 写盘
```

原理很简单：jieba 默认词典里查不到的词，就是这圈子自己造的（游戏角色、战术、群友外号）。按「出现次数 + 说过的人头数」筛一遍，剩下的基本就是黑话。挖出来的词按群分开存，注入时只给当前群的——A 群的黑话喂给 B 群，模型只会乱用。

两层都在 prompt 里以「怎么用」的形式出现（「贴切才接，最多用一个」「不懂别硬接」），不是让模型背解释。

## 控制台

装上插件后，AstrBot 面板里会多一个 **大肥鱼控制台**（插件 → 小鲸鱼·群聊拟真 → 页面）。页面跟面板同一套登录态，不用另开端口、不用另设密码。

里面是五块：运行状态和开关汇总、每个群的冷热场（接话开关、插话意愿、心境三维、最近说话时间）、群友好感度、群友记忆排行、最近交互事件流。

改群只在页面里动三项：`enabled`（这个群接不接）、`interject_desire`（这个群多爱插话，0~1）、`note`（备注）。这三项写进插件目录的 `data/group_overrides.json`，和 AstrBot 全局配置分开——单个群调错不会把整份配置写坏。

后端在 `fish_core/panel.py`，八个接口挂在 `/astrbot_plugin_fish/panel/*`；页面是 `pages/console/index.html`，一个文件，没有构建步骤。

## 记忆查询

群里或私聊发 `/memary`：

- 普通群友只能看自己的；
- 群主、管理员、机器人主人能看全群；
- 人多时走合并转发，协议端不支持就退分页文本。

它只读 `astrbot_plugin_fish/data/fish_memory.db` 和 `favor_state.json`，没有任何写操作，所以聊天插件宕了它照样能用。旧版机器人的 `user_memories.json` 是可选的历史补充，`user_mem_path` 留空就不读。

## 关于数据

仓库里的群号、QQ 号、昵称全是占位符（`999999`、`张三`、`100012345` 这类），示例对话也是编的。

真实运行数据在插件各自的 `data/` 下——记忆库、心境快照、群表达样例、prompt 存档——已列进 `.gitignore`。API key 一律从环境变量或 AstrBot 面板读，仓库里没有密钥。

提交前会跑一道隐私扫描：

```bash
./scripts/install_hooks.sh   # 装到 .git/hooks/pre-commit
python3 scripts/privacy_scan.py --all --audit   # 手动查全仓
```

密钥、手机号、内网 IP、家目录路径会直接拦；9~11 位数字（QQ 号量级）先提醒，确认是编的写进 `.privacy-allowlist` 就不再报。

## CI

每次推送跑三件事：`compileall` 语法检查、`scripts/check_plugin.py` 结构自检（metadata 字段、配置项类型和描述、入口是否像插件）、`privacy_scan.py` 全仓隐私扫描。Python 3.10 和 3.12 各跑一遍。

## 许可

MIT。人设和示例对话随便改，改成自己的角色就行。
