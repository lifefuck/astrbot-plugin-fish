# QQ 群聊机器人 · AstrBot 插件

[![ci](https://github.com/lifefuck/astrbot-plugin-fish/actions/workflows/ci.yml/badge.svg)](https://github.com/lifefuck/astrbot-plugin-fish/actions/workflows/ci.yml)

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
- **接话时机**：先按「像不像在问人 / 有没有空档 / 是不是重复」打分，过门槛才动手；再加一道社会判断，判不接就一次模型都不调。
- **反模板**：起手撞车、结尾撞车、连续重复语气词、复述式开场，出口统一削掉。
- **手滑**：偶尔打错字，小概率再补一条纠正。
- **拆条**：超过 60 字拆成 2~3 条，间隔随机补发，不在括号和标签中间切。
- **表情包**：按情绪标签选图，一回合最多一张；刚发过、对方刚发图都拦。
- **看图**：群友发图 → 缩图 → Gemini 白描 → 描述塞进 prompt，失败静默降级。
- **跨群**：别的群聊了什么留一句短摘要，必要时可以顺口提一句。
- **戳一戳**：被戳先回戳，再正常回一句话。

## 装

前置：AstrBot 4.x 跑着，NapCat 已连上 AstrBot 的反向 WebSocket。

```bash
git clone https://github.com/lifefuck/astrbot-plugin-fish.git
cd astrbot-plugin-fish

cp -r astrbot_plugin_fish        /path/to/astrbot/data/plugins/
cp -r astrbot_plugin_memory_cmd  /path/to/astrbot/data/plugins/

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

模型（provider）、唤醒前缀 `/` 属于 AstrBot 全局设置，不在插件里配。

表情包是个可选依赖：把表情包目录放在插件目录下的 `meme-pack/`，索引会自动载入；没有就只发文字。

## 控制台

装上插件后，AstrBot 面板里会多一个 **大肥鱼控制台**（插件 → 小鲸鱼·群聊拟真 → 页面）。页面跟面板同一套登录态，不用另开端口、不用另设密码。

里面是四块：运行状态和开关汇总、每个群的冷热场（接话开关、插话意愿、心境三维、最近说话时间）、群友记忆排行、最近交互事件流。

改群只在页面里动三项：`enabled`（这个群接不接）、`interject_desire`（这个群多爱插话，0~1）、`note`（备注）。这三项写进插件目录的 `data/group_overrides.json`，和 AstrBot 全局配置分开——单个群调错不会把整份配置写坏。

后端在 `fish_core/panel.py`，五个接口挂在 `/astrbot_plugin_fish/panel/*`；页面是 `pages/console/index.html`，一个文件，没有构建步骤。

## 记忆查询

群里或私聊发 `/memary`：

- 普通群友只能看自己的；
- 群主、管理员、机器人主人能看全群；
- 人多时走合并转发，协议端不支持就退分页文本。

它只读 `astrbot_plugin_fish/data/fish_memory.db`，没有任何写操作，所以聊天插件宕了它照样能用。

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
