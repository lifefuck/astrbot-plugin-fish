# astrbot_plugin_fish —— 把「小鲸鱼」的对话能力搬进 AstrBot

从仓库根目录那套自研机器人移植。**不改 AstrBot 源码**，全部逻辑在插件目录内，`git -C /path/to/astrbot status --porcelain` 保持干净。

- 插件目录：`data/plugins/astrbot_plugin_fish/`（AstrBot 根目录下）
- 运行状态：`data/fish_memory.db`（记忆）、`data/affect_state.json`（心境）、`data/style_fewshot.json`（群表达样例）、`data/prompt_archive/`（调试用 prompt 存档）、`data/cross_group_topics.json`（跨群话题短摘要）、`data/peer_bots.json`（别的机器人 QQ 名单）
- 运行时配置：`data/config/astrbot_plugin_fish_config.json`（AstrBot 自己生成，**带 BOM**，读写一律 `utf-8-sig`）

## 1. 能力对照表

| 能力 | 原项目模块 | 插件落点 | 说明 |
|---|---|---|---|
| 人设全文（1223 字） | `prompts.py: SYSTEM_PROMPT` | `fish_core/prompts.py`（原件复制） | 一字未改 |
| 身份/时间锚点、群表达 few-shot、时间线拼装 | `services/llm.py` | `fish_core/composer.py` | 自研 port，`build_system_prompt()` / `time_hint()` / `sanitize_reply()` |
| 轻量心境（valance/arousal/pout/care） | `services/affect.py` | `fish_core/affect.py`（原件复制） | 每条群消息 `on_group_message`，回完 `on_bot_spoke`，`prompt_memo()` 注进 prompt |
| 长期记忆（事实/偏好/计划/事件 7 类规则抽取 + 召回） | `services/fish_memory.py` | `fish_core/fish_memory.py`（原件复制） | 每条群消息自动感知；回话前 `recall(top_k=2)` |
| 群表达风格学习 | `services/style_learner.py` | `fish_core/style_learner.py` | `observe()` 逐条累积，池上限 200 |
| 反模板（起手/收尾撞车、重复语气词、复述式开场） | `services/anti_template.py` | `fish_core/anti_template.py` | 出口钩子里 `match→reshape→trim_particle→strip_echo_opener` |
| 手滑错字 + 自纠 | `services/typo.py` | `fish_core/typo.py` | 改完再补一句「改一下，X」 |
| 接话质量分 / 时机修正 | `bot.py: score_message_quality` + `INTERJECT_*` 词表、`services/interject_planner.py` | `fish_core/interject_score.py` + `fish_core/interject_planner.py` | 抽成纯函数，阈值 `0.30 + 0.32*desire` |
| 表情包选图 | `meme_catalog.py` + `meme-pack/` | `fish_core/meme_catalog.py` + 符号链接 `meme-pack` | 138 张；`[MEME:xx]` 标签在出口处剥掉，只发图不发标签 |
| 该不该开口 | `handlers/group.py` + `bot.py` 判定段 | `main.py: on_group()` | 见下节 |
| 长回复拆条 | `services/pacing.py` + `handlers/group.py` 拆条发送 | `fish_core/splitter.py` + `main.py: on_sent()` | 超阈值拆 2~3 条，间隔 0.6~1.5s 随机补发；括号/标签内不切 |
| Gemini 看图 | `services/vision.py` | `fish_core/vision.py` | 群友发图 → 缩到 640px → gemini-3.8-flash 白描 → 注入 prompt；失败静默降级 |
| 两步生成（judge） | `services/social_read.py` | `fish_core/social_read.py` | 先判「在聊什么、该不该接、怎么接」，判不接直接 stop，不调主模型 |
| 斗图闸 | `services/messaging.py` 表情连发控制 | `main.py: _meme_gate()` | 一回合最多一张；刚发过/群友刚发图/同一标签连发都拦 |
| 跨群记忆 | `cross_memory.py` | `fish_core/cross_memory.py` | 各群话题存短摘要，可选择性引用「别的群在聊」（默认开，最多 2 条、3 小时内） |
| 别的机器人识别 | `bot.py: load_peer_bots/is_peer_bot` | `fish_core/peer_bots.py` | 名单 + 昵称特征；机器人的消息不接、不进时间线 |

复制而非依赖外部路径，插件自包含；`meme-pack` 用符号链接指回源仓库以免复制几百 MB。

## 2. 消息流（比原项目多出来的部分：AstrBot 的唤醒机制）

AstrBot 默认只把「@我 / `ab!` 前缀 / 引用我 / 私聊 / 插件 filter 通过」的消息放进管道。本插件用最后一条：群 handler 的 filter **永远通过**，于是能看到群里每一条消息，再自己决定：

```
群里每条消息
  ├─ 不是本群（enabled_groups 白名单）→ return，不碰
  ├─ 以 ab! 开头 → return（交给 AstrBot 原生 LLM 链路，实测正常工作）
  ├─ 以 # * % ~ 开头 → return（面板指令，静默；故意不 stop_event，别掐死别的插件）
  ├─ 发送者是别的机器人（peer_bots 名单 / 昵称特征）→ return，不接、不进时间线
  ├─ 记账：时间线 / 心境 / 风格池 / 跨群话题摘要
  ├─ 被 @ 或 @全体 → 必回
  └─ 否则打分 score_message_quality × 时机修正
        ├─ 未过门槛 / 冷却中 / 复读 / 每小时上限 → stop_event()（但仍写长期记忆）
        └─ 过门槛 → ↓
             ① 并发：社会判断（social_read）+ 图片白描（vision）
             ② judge 判「不接」→ stop_event()，主模型一次都不调
             ③ 判「接」→ 把结论 + 图片描述 + 跨群话题拼进 prompt，yield request_llm
```

出口（`on_decorating_result` → `after_message_sent`）：

```
清洗 → 反模板 → 手滑 → 拆条（>60 字拆 2~3 条）
     → 斗图闸（一回合最多一张）→ 先发第一条
     → after_message_sent 里按 0.6~1.5s 随机间隔补发其余条 → 最后附图
```

回复后处理走 `@filter.on_decorating_result()`：只处理带 `fish_pending` 标记的回复，**ab! 唤醒的原生回复完全不碰**（避免把 AstrBot 自己的回答改成鲸鱼腔）。

## 3. 安装 / 启用

```bash
# 已就位，重启即可
systemctl --user restart astrbot
journalctl --user -u astrbot -f --no-pager | grep fish
```

启动日志会打印：`[fish] 插件已加载：记忆库 …，心境 …，表情包 138 张`。

`enabled_groups` 默认 `[]`＝**对所有群静默**（安全默认，装完不会往真群发消息）。上线时改成真实群号：

```json
"enabled_groups": ["123456789"]
```

`*` 表示所有群都生效。

## 4. 调参

| 配置项 | 默认 | 作用 / 怎么调 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `enabled_groups` | `[]` | 生效群白名单，空＝全关，`*`＝全开 |
| `bot_nickname` | `小鲸鱼` | 只用于 prompt 里锚定身份，防止模型把自己的名字当成别人 |
| `wake_prefix` | `ab!` | 与 AstrBot 全局一致；这类消息插件完全不插手 |
| `interject_desire` | `0.30` | 被动接话意愿。门槛 = `0.30 + 0.32×desire` → 0.30 对应 0.62。`0` ≈ 只在被 @ 时说话；`0.8` 门槛 0.56 明显话多 |
| `interject_hourly_cap` | `8` | 每群每小时主动接话上限 |
| `interject_cooldown_sec` | `300` | 说完一句后的硬冷却。**它也同时改写 `interject_planner.COOLDOWN_SEC`**，否则会出现「硬冷却放行、软权重还按 300 秒打折」的矛盾 |
| `reply_when_at` | `true` | 被 @ 是否必回 |
| `reply_private` | `true` | 私聊是否用同一套人格 |
| `typo_prob` | `0.06` | 手滑概率，只改长句、不碰链接/@/数字 |
| `typo_correct_prob` | `0.30` | 手滑后再补一句纠正的概率 |
| `meme_enabled` | `true` | 是否配表情包 |
| `timeline_size` | `12` | 塞进 prompt 的群聊条数 |
| `memory_recall_topk` | `2` | 每人召回几条长期记忆 |
| `style_learning` | `true` | 是否学习群表达 |
| `debug_dump_prompt` | `false` | 落盘最终 prompt 到 `data/last_prompt.txt` + `data/prompt_archive/`（保留最近 50 份）。排查「心境/记忆到底进没进」时打开 |
| `command_prefixes` | `["#","*","%","~"]` | 面板指令前缀，这些消息静默放行 |
| `split_enabled` | `true` | 长回复拆条总开关 |
| `split_max_chars` | `60` | 单条超过多少字才拆 |
| `split_max_parts` | `3` | 最多拆几条，多的并进最后一条 |
| `split_gap_min` / `split_gap_max` | `0.6` / `1.5` | 两条之间的随机间隔（秒） |
| `vision_enabled` | `true` | 发图时是否让 Gemini 看一眼 |
| `vision_model` | `gemini-3.8-flash` | 视觉主模型 |
| `vision_fallback_models` | `["gemini-3.5-flash-lite","gemini-3.6-flash"]` | 降级链；**上次成功的模型下次自动排到队首** |
| `vision_timeout_sec` / `vision_total_budget_sec` | `6` / `12` | 单模型超时 / 整张图总预算，超了就按没图继续 |
| `vision_max_images` | `2` | 单条消息最多看几张 |
| `vision_proxy` | `http://127.0.0.1:7890` | 访问 Google 的代理，留空=直连 |
| `vision_api_key` / `vision_key_env_file` | `""` / `~/.config/qq-deepseek-bot.env` | 密钥来源；优先级 环境变量 > env 文件 > 配置 |
| `social_read_enabled` | `true` | 两步生成开关 |
| `social_read_provider` | `""` | judge 用哪个 provider（空=当前会话默认） |
| `social_read_timeout_sec` | `8` | judge 超时；超时/报错一律按「能接」放行 |
| `meme_gate_enabled` | `true` | 斗图闸开关 |
| `meme_min_gap_sec` | `300` | 两张表情包最小间隔 |
| `meme_follow_gap_sec` | `90` | 群友发图后多久内不跟图 |
| `peer_bots_path` | `""` | 机器人名单路径（空=`data/peer_bots.json`，不存在会自动建 `[]`） |
| `peer_bots_name_hints` | `[]` | 追加的机器人昵称特征（默认已含 机器人/bot/助手/管家） |
| `cross_group_enabled` | `true` | 跨群记忆开关 |
| `cross_group_recall` | `2` | 引用几条别的群话题 |
| `cross_group_max_topics` | `60` | 话题库上限 |

## 5. 实测（注入法，未向任何真实 QQ 群发消息）

验证手段：自写 `scripts/inject.py` 冒充 NapCat 连 `ws://127.0.0.1:6199/ws`（AstrBot 的 OneBot 反向 WS 服务端），往假群 `999999` 注入事件，机器人的 `send_group_msg` 回到这条测试连接上。

```bash
cd /path/to/astrbot
cp data/plugins/astrbot_plugin_fish/scripts/test_config.json data/config/astrbot_plugin_fish_config.json
systemctl --user restart astrbot && sleep 13
./.venv/bin/python data/plugins/astrbot_plugin_fish/scripts/inject.py                     # 全量（约 6 分钟）
./.venv/bin/python data/plugins/astrbot_plugin_fish/scripts/inject.py --only at image     # 挑场景，可写多个
./.venv/bin/python data/plugins/astrbot_plugin_fish/scripts/inject.py --only long meme    # 长回答拆条 + 斗图闸
```

`test_config.json` 就是下面这轮实测用的参数（`interject_cooldown_sec: 30`、`typo_prob: 0`、`debug_dump_prompt: true`），生产参数见第 4 节。

### 5.1 真实发送内容（`send_group_msg` 原样）

| # | 注入 | 是否回 | 机器人实际发出 |
|---|---|---|---|
| 0 | `我明天下午要去面试，紧张死了`（张三） | 不回 | — |
| 1 | `@小鲸鱼 你还记得我明天要干嘛吗` | **回** | `面试啊 明天下午\n\n紧张个啥，睡一觉就过去了` |
| 2 | `今天好热啊` | 不回 | — |
| 2b | `太强了哈哈哈` ×6 | 不回 | — |
| 2c | `@小鲸鱼 你今天怎么这么有精神` | **回** | `心情好呗 睡了整个下午` |
| 3 | `哈哈` / `哈哈` / `哈哈哈哈` / `草` | 不回 | — |
| 4 | `ab! 用一句话解释什么是快速排序` | AstrBot 自己回 | `快速排序：任取一个元素作基准…（标准教科书腔，无鲸鱼人设 = 证明没被插件接管）` |
| 5 | `有人知道这个报错咋整吗？pip 装到一半就挂了` | **主动接话** | `换源吧` |
| 6 | `我也遇到了，谁知道怎么解决？` | 不回 | — |
| 7 | `#帮助` | 不回 | — |
| 8 | 私聊：`在吗，问你个事` | **回** | `在啊，说`（`send_private_msg`） |

场景 9~15（发图 / 长回答拆条 / 斗图闸 / 别的机器人 / 两步生成判不接 / 跨群记忆）是后加的，输出见 5.5。
注入用的图是 `scripts/assets/probe_card.png`（自己画的通知卡片，图上文字固定，便于核对 OCR 有没有进 prompt）。

### 5.2 判定日志（`journalctl --user -u astrbot | grep fish`）

```
[fish] 插件已加载：记忆库 …/data/fish_memory.db，心境 …/data/affect_state.json，表情包 138 张
[fish] 群 999999 接话质量 0.34 vs 门槛 0.62（普通闲聊 | 时机 冷场）-> 略过
[fish] 群 999999 张三(10001) 决定回（被@）：@小鲸鱼 你还记得我明天要干嘛吗
[fish] 群 999999 不接（刚说过话，冷却中）：今天好热啊
[fish] 群 999999 不接（同一句复读）：太强了哈哈哈
[fish] 群 999999 李四(10002) 决定回（被@）：@小鲸鱼 你今天怎么这么有精神
[fish] 群 999999 接话质量 0.21 vs 门槛 0.62（纯水+热场 | 时机 热聊中）-> 略过
[fish] 群 999999 接话质量 0.27 vs 门槛 0.62（话题延续+纯水+热场 | 时机 热聊中）-> 略过
[fish] 群 999999 接话质量 0.06 vs 门槛 0.62（话题延续+过短+纯水+热场 | 时机 热聊中）-> 略过
[fish] 群 999999 接话质量 0.96 vs 门槛 0.62（有内容+提问+技术+话题延续+热场 | 时机 热聊中）-> 接
[fish] 群 999999 赵六(10004) 决定回（主动接话 0.96/0.62）：有人知道这个报错咋整吗？pip 装到一半就挂了
[fish] 群 999999 不接（刚说过话，冷却中）：我也遇到了，谁知道怎么解决？
```

### 5.3 记忆/心境确实进了最终 prompt

`data/prompt_archive/20261115-222056_10001.txt`（场景 1 的实际 prompt）：

```
【你还记得的关于他/她的事】：
- 我明天下午要去面试，紧张死了

【此刻的感觉（只影响语气和长短，别把这段说出来，别提任何分数或状态名）】：心情挺好，话比平时多一点，接得住梗

【当前真实时间】：2026-11-15 22:20 星期日（晚上）
```

注意：**先把这条消息写进记忆再召回**是不行的——当前这句会被自己捡回来，prompt 里出现「你还记得我明天要干嘛吗」自指。代码里是「先 `recall` 算 prompt，再 `record_turn`」，并且召回到的新条目会跳过与当前文本互为子串的项。

### 5.4 出口加工不泄露标签

模型这次带了标签，插件在出口剥掉标签、附图，聊天文本里看不到 `[MEME:xx]`：

```
[fish] 群 999999 张三(10001) 决定回（被@）：@小鲸鱼 你还记得我明天要干嘛吗
[fish] 按标签 得意 配了一张表情包
Prepare to send - 张三/10001: 面试啊，明天下午的
这不刚说完就忘不了
紧张啥，去了先把话说明白就行 [图片]
```

最后那一轮全量实测里模型没吐标签，也就没有附图（不硬凑图），文本同样干净。

```
--- 1. 表情标签剥离 + 出口清洗 ---
模型原文 : **[MEME:得意]** 那必须的（笑）`真的`
抽出标签 : 得意
清洗后   : 那必须的真的
--- 2. 反模板：起手撞车 ---
命中原因 : opening
削形后   : 我昨天就说过，这事早定了
--- 4. 手滑与纠正（prob=1.0 强制触发）---
打错后   : 我觉得应该已经知道了，现在还是先一起看看题
纠错话术 : 手滑，看问
```

手滑链路在真实运行里也验过（临时把 `typo_prob`、`typo_correct_prob` 都设 1.0）：

```
<- 动作：send_group_msg msg='就潜水呗，偶尔冒两颗泡损人两句，然候……'
<- 动作：send_group_msg msg='改一下，然后'
```

### 5.5 新增六项能力的实测（注入法，不往真群发）

> 坑先记在这：注入事件的 `self_id` **必须是假号**（`1000000001`），用机器人真号时带 `@` 的事件会被
> aiocqhttp 适配器当成"自己发的"直接吞掉，永远进不了 pipeline；`message_id` 每条唯一。

**① 发图 → Gemini 白描 → 进 prompt**

```
[fish] 群 999999 social_read（2.5s）判=接 话题='确认要准备什么' 怎么接='先问清"这个"具体指什么…'
[fish] 视觉 gemini-3.5-flash-lite 出描述（1.9s，key 来自 envfile）：白底黑框内写着通知内容。具体文字为：面试安排 / 周三 14:00 线上二面 / 面试官：王工 / 记得带上作品集
[fish] 群 999999 图片描述：白底黑框内写着通知内容…
```

实际 prompt（`data/prompt_archive/…_10009.txt`），描述插在「当前这句」正上方，不能放最前面（放前面模型会当成背景设定照着聊）：

```
【他这张图里】：白底黑字的通知图片，四周带有细边框。图中有四行文字：面试安排、周三 14:00 线上二面、面试官：王工、记得带上作品集。

【当前这句】：图图（10009）：@小鲸鱼 你看下这个，我该准备啥
```

模型确实用上了图里的内容（"周三 / 王工 / 作品集"都只存在于图上）：

```
<- send_group_msg: '周三线上二面，还是王工

作品集先整理好，提前传一份或者备好屏幕共享'
<- send_group_msg: '再想想他可能问的项目细节，你自己做过的东西得能说清

摄像头麦克风今晚就试一遍，别到点了翻车'
```

顺带一条实测结论：`gemini-3.8-flash` 在这台机器上识一张图要 25 秒还会超时，`gemini-3.5-flash-lite`
1.9 秒就把四行字逐字抄准。所以默认主模型改成 lite，3.8 退到降级链末尾。

**② 长回复拆条（真实多条 `send`）**

```
[fish] 长回复拆成 3 条（63 字）：还能为啥，吃出来的呗 / 一开始他们管我叫大肥鱼， / 嫌太难听才改的小鲸鱼
[fish] 拆条第 2/3 条补发（间隔 0.9s）：一开始他们管我叫大肥鱼，
[fish] 拆条第 3/3 条补发（间隔 0.8s）：嫌太难听才改的小鲸鱼
[fish] 拆条后补发表情包
```

对应注入端看到的四发（上面那轮把 `split_max_chars` 临时压到 20 才切得开；默认 60 时同一场景切成 2 条）：

```
<- 动作：send_group_msg msg='还能为啥，吃出来的呗'
<- 动作：send_group_msg msg='一开始他们管我叫大肥鱼，'
<- 动作：send_group_msg msg='嫌太难听才改的小鲸鱼

我这体型往那一站…'
<- 动作：send_group_msg msg='[image]'
```

**③ 两步生成：先判后答，判不接就不调主模型**

```
[fish] 群 999999 接话质量 0.56 vs 门槛 0.40（有内容+话题延续 | 时机 无修正）-> 接
[fish] 群 999999 social_read（1.5s）判=不接 话题='追问项目是否上线' 怎么接='' | 原句：@李四 你那个项目最后上线了没
[fish] 群 999999 social_read 判不接（话题='追问项目是否上线'），不调主模型：@李四 你那个项目最后上线了没
<- 动作：无（插件未触发回复）
```

（这一轮把 `interject_desire` 临时调到 `0.78`，否则那句 @ 李四 会先被质量分闸门以 0.42/0.62 拦掉，
根本走不到 judge。生产默认 0.30 时，两层闸门前置的那个更容易先命中。）

第一行是原来的质量分闸门（它放行了），第二、三行才是 judge 拦下来的——两层是串联的，
judge 只在"已经决定要说点什么"之后才跑，不会给每条消息都白烧一次调用。
判「接」时结论会以 `【这轮在聊】/【怎么接】` 两行进 prompt（见下），但不贴 JSON，免得模型学成结构化输出。

**④ 斗图闸：一回合一张**

```
[fish] 按标签 无奈 配了一张表情包
[fish] 表情包闸拦住（距上一张表情包 33s，不足 300s），这轮纯文字
<- 动作：send_group_msg msg='图真发不出来了，相册都翻空了，你点个题我给你现编一个'
```

三条判定（间隔 / 群友刚发图 / 同标签连发）在 `fish_core/meme_gate.py` 里是纯函数，
`selfcheck.py` 第 7 段把四种情况全覆盖，改阈值不会悄悄坏掉。

**⑤ 别的机器人：不接、不记账**

名单 `data/peer_bots.json`（一个 QQ 号数组，不存在会自动建成 `[]` 并打日志）。名单里的号发来的
高分提问（0.78 的 desire 下必过的分数）连闸门都不进：

```
[fish] 群 999999 忽略机器人消息：小助手(10088) 有人知道这个报错咋整吗？pip 装到一半
<- 动作：无（插件未触发回复）
```

**⑥ 跨群记忆：可以顺口提一句别的群在聊什么**

A 群聊一句 → 记成短摘要；B 群 @ 一次，prompt 里多出：

```
【别的群此刻在聊】（不是这个群的事，只在你确实接得上时顺口带一句，别硬提）：
- 另一个群在聊「我们组周五团建去爬山，谁去报名」
```

回复没有硬提团建（"周末都快过完了你才问，躺两天就没了，你呢"），符合"允许提、不强制提"的设计。
开关 `cross_group_enabled` 默认开，最多引 `cross_group_recall=2` 条、只引 3 小时内的、
URL/@/5 位以上长号码在写入前就洗掉。

### 5.6 自检脚本

```bash
./.venv/bin/python data/plugins/astrbot_plugin_fish/scripts/selfcheck.py
```

覆盖 12 段：标签剥离/清洗、反模板三种命中、口癖与复述开场、强制手滑+纠正话术、5 组质量分与门槛对比、
拆条（长/中/碎片三例）、斗图闸四种情形、视觉降级链、judge 结论解析（含坏 JSON 兜底）、跨群摘要清洗与自群屏蔽、
机器人识别、时间/身份锚点。不需要 AstrBot 进程、不联网。

## 6. 回滚

```bash
# 1) 立刻停用（不必重启服务也生效：AstrBot 面板里关插件，或直接改配置）
#    把 data/config/astrbot_plugin_fish_config.json 里 "enabled" 改 false 并重启
# 2) 或者整个移除
systemctl --user stop astrbot
mv /path/to/astrbot/data/plugins/astrbot_plugin_fish ~/astrbot_plugin_fish.bak
systemctl --user start astrbot
```

- 插件不写 AstrBot 核心，不注册全局命令，不改 `data/cmd_config.json`，不碰 NapCat。
- 想连记忆/心境一起清空：删 `data/plugins/astrbot_plugin_fish/data/` 下 `fish_memory.db`、`affect_state.json`、`prompt_archive/` 即可（首次启动会重建）。
- `fish_core/meme-pack` 只是符号链接，删插件目录不会动到源仓库的表情包。

## 7. 已知差异 / 坑

1. **拆条是"先发第一条、剩下在 `after_message_sent` 里补发"**：AstrBot 一次只让改一个 `MessageEventResult`，
   所以补发必须走 `event.send()`。**补发前先把 `fish_tail` extra 取空**，否则补发那几条会再次触发
   `after_message_sent`，变成自我复制的无限连发（写的时候踩到过）。
2. **`@` 判定只看真 `At` 组件**：注入时把 "@小鲸鱼" 写进文本不算被 @，只能靠质量分过线，冷却期还会被拦住。
   做注入测试一定要构造 `{"type":"at","data":{"qq":<self_id>}}`，并且 `self_id` 用假号。
3. **`ab!` 前缀消息完全不插手**，包括出口加工——AstrBot 原生回答不会带鲸鱼腔，这是刻意分工。
4. **面板指令只 `return` 不 `stop_event`**：`stop_event()` 会把下游其他插件的同名指令一起掐死，改成静默放行后实测 AstrBot 也不会因此回一句废话。
5. **冷却位在「决定回」时就占住**：模型一次要跑十几秒，等回复真正发出才记账的话，同一波刷屏会被判断成「还没说过话」连回三次（实测踩到过，回复三连发）。
6. **复读判定看同一个人连续两句相同**：不然刷「太强了哈哈哈」这种有情绪的长句会稳定过线。
7. **配置 JSON 带 BOM**：`json.load` 会直接抛 `Unexpected UTF-8 BOM`，脚本里一律 `encoding='utf-8-sig'` 读、`utf-8` 写。
8. **记忆读写在 `asyncio.to_thread` 里跑**：sqlite 是同步 IO，放事件循环里会卡住整条管道。
9. **`interject_planner.adjust()` 读模块级 `COOLDOWN_SEC`**，不是参数；改了配置必须同步写回模块属性，否则软权重会一直按 300 秒打折。
10. 每条群消息都会写长期记忆（与原项目一致），所以测试群的假数据也会进 `fish_memory.db`；换真群前建议清库。
