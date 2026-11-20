# 配置参考

本文件由 `scripts/gen_config_doc.py` 从 `_conf_schema.json` 生成，**不要手改**。
改配置项后跑一次生成脚本；CI 会检查它和 schema 是否一致。

面板里每一项都带 `hint`，这里只做速查。默认值列里的 `-` 表示默认空字符串。

| 配置项 | 默认 | 说明 |
| :--- | :--- | :--- |
| `enabled` | `true` | 总开关。关掉后插件完全不介入，群里恢复 AstrBot 原生行为（只有 @ 和唤醒前缀会回）。 |
| `enabled_groups` | `[]` | 生效群白名单。留空 = 所有群都不生效（安全默认）。填群号才上线，填 ["*"] 对所有群生效。 |
| `bot_nickname` | `小鲸鱼` | QQ 上的昵称。只在 prompt 里锚定身份用，避免模型把自己的名字当成别人。 |
| `wake_prefix` | `ab!` | AstrBot 唤醒前缀（仅用于识别，不拦消息）。带这个前缀的消息完全交给 AstrBot 自己的 LLM 链路处理，小鲸鱼不插手。必须和 AstrBot 全局唤醒前缀一致。 |
| `interject_desire` | `0.3` | 主动接话意愿。越高门槛越低。0.30 → 质量门槛 0.62（普通闲聊不接，提问/玩梗/点名才接）；0.60 → 门槛 0.38（几乎每条都插一句）。 |
| `interject_hourly_cap` | `8` | 每小时主动插话上限。被 @ 回话不受这个上限约束。 |
| `interject_cooldown_sec` | `120` | 主动接话硬冷却（秒）。刚说完话这段时间内绝不再主动插嘴，防霸屏。 |
| `reply_when_at` | `true` | 被 @ 必回 |
| `reply_private` | `true` | 私聊必回 |
| `meme_enabled` | `true` | 按情绪标签配表情包。模型写了 [MEME:情绪] 时附一张图；标签本身一定被剥掉，不会漏进群。 |
| `timeline_size` | `40` | 上下文条数上限（配合 context_window_sec 时间窗，先按时间筛再截条数） |
| `memory_recall_topk` | `4` | 记忆召回条数 |
| `style_learning` | `true` | 在线学习群表达样例。把群里相邻两条消息凑成「有人说→别人怎么接」，进 data/style_fewshot.json。 |
| `debug_dump_prompt` | `false` | 把每次拼好的 prompt 落盘。写到 data/last_prompt.txt，用于核对心境/记忆提示是否被注入。排查完记得关。 |
| `command_prefixes` | `["#", "*", "%", "~"]` | 其他插件的指令前缀。以这些字符开头的群消息（云崽面板指令）完全不介入。 |
| `split_enabled` | `true` | 长回复拆条。超过字数阈值就拆成 2~3 条隔 0.6~1.5 秒补发，像人连着敲。括号和表情标签不会被切断。 |
| `split_max_chars` | `26` | 拆条字数阈值。单条超过这么多字就拆成两条。真人一条消息通常十几二十字，超过这个数就该换行了。0 或负数 = 永不拆。 |
| `split_max_parts` | `3` | 最多拆成几条。超出的内容并进最后一条，宁可长一点也不刷屏。 |
| `split_gap_min` | `0.6` | 拆条最小间隔（秒） |
| `split_gap_max` | `1.5` | 拆条最大间隔（秒） |
| `vision_enabled` | `true` | 群里发图时让 Gemini 看一眼。只在决定要回的时候才识图，不会为每张图烧配额。识图失败静默降级成纯文字。 |
| `vision_model` | `gemini-3.5-flash-lite` | 视觉主模型。默认 lite：实测 1~2 秒就能把图里的字逐字抄准；gemini-3.8-flash 在这台机器上常 25 秒超时，不适合群聊实时场景。 |
| `vision_fallback_models` | `["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]` | 视觉降级模型。只放 lite 系：实测 3.5/3.1-flash-lite、flash-lite-latest 都在 1~3 秒出描述；3.6/3.8-flash 同样一张图要 14~21 秒，配 12 秒总预算等于永远轮不上。 |
| `vision_max_images` | `2` | 单条消息最多看几张图 |
| `vision_timeout_sec` | `12` | 单模型超时（秒）。群聊等不起，超时立刻换下一个模型。 |
| `vision_total_budget_sec` | `20` | 识图总预算（秒）。一张图最多等这么久，超了就按没有图继续回。 |
| `vision_proxy` | `http://127.0.0.1:7890` | 访问 Google 的代理。留空 = 直连。国内机器基本都得填。 |
| `vision_api_key` | - | Gemini 密钥（可选）。优先级：环境变量 > vision_key_env_file > 这里。建议留空，用 env 文件。 |
| `vision_key_env_file` | `~/.config/qq-deepseek-bot.env` | 读取密钥的 env 文件。按 KEY=VALUE 解析，只取 GEMINI_API_KEY / GOOGLE_API_KEY / VISION_*。 |
| `social_read_enabled` | `true` | 两步生成：先判该不该接。先小调用判「这帮人在聊什么、我该不该接」，判不接就不调主模型。 |
| `social_read_provider` | - | judge 用的 provider id。留空 = 当前会话默认模型。填了就用指定 provider 省主模型额度。 |
| `social_read_timeout_sec` | `8` | judge 超时（秒）。超时/报错一律按「能接」放行，绝不因此把回复吞掉。 |
| `meme_gate_enabled` | `true` | 斗图闸。同一话题一回合最多一张：刚发过、群友刚发图、标签和上次相同都拦。 |
| `meme_min_gap_sec` | `180` | 两张表情包之间的最小间隔（秒）。防止连着几轮都在甩图。 |
| `meme_follow_gap_sec` | `60` | 对方发图后多久内不跟图（秒）。跟图就成斗图刷屏了。 |
| `peer_bots_path` | - | 其他机器人名单路径。data/peer_bots.json，一个 QQ 数组。留空 = 插件 data/peer_bots.json；文件不存在会自动建空数组。 |
| `peer_bots_name_hints` | `[]` | 机器人昵称特征（追加）。默认已含 机器人/bot/助手/管家，这里只补额外的。 |
| `cross_group_enabled` | `true` | 跨群记忆。把各群话题记成短摘要，拼 prompt 时允许提一句「别的群在聊」。 |
| `cross_group_recall` | `2` | 引用几条别的群话题。只引 3 小时内、去重后的短摘要。 |
| `cross_group_max_topics` | `60` | 话题库上限 |
| `poke_back` | `true` | 被戳时回戳。有人戳它，先秒戳回去再回话。同一个 3 秒内只回戳一次，防止互戳死循环。 |
| `owner_qq` | - | 主人 QQ，/off /on /memes 除群管外也放行；留空则只认群管 |
| `bot_ban_guard` | `true` | 机器人被禁言时不调模型（照样收得到消息，白烧 token） |
| `density_guard` | `true` | 刷屏闸：两分钟内主动说过 N 条就不再主动搭腔（被 @ 不受限） |
| `density_window_sec` | `120` | 刷屏闸的统计窗口（秒） |
| `density_limit` | `2` | 窗口内最多主动说几条 |
| `connectors_enabled` | `true` | 被问到出处/最新消息时才联网检索 |
| `enable_games` | `true` | 文字游戏：数字炸弹、掷骰子、抽签 |
| `meme_battle_enabled` | `true` | 有人 @ 它还甩图时斗图反杀 |
| `media_parse_enabled` | `true` | 解析消息里的 B 站/GitHub/音乐/短视频链接 |
| `meme_pool_size` | `30` | 热梗池每轮注入几条 |
| `meme_rotation_sec` | `1800` | 热梗池多久换一轮（秒） |
| `follow_window_sec` | `300` | 被叫过后免 @ 追问窗口（秒） |
| `follow_max_turns` | `2` | 追问窗口里最多连续免 @ 几次 |
| `follow_min_gap_sec` | `8` | 追问窗口内两次回复的最小间隔（秒）,防止一句一回刷屏 |
| `panel_event_buffer` | `300` | 控制台事件流缓冲多少条 |
| `admin_actions_enabled` | `true` | 自然语言处置人：群管说「把某人禁言十分钟」「踢了他」会真执行 |
| `yunzai_sync` | `true` | /off 时把本群从云崽白名单摘掉，/on 加回来 |
| `yunzai_other_yaml` | - | 云崽 other.yaml 路径，留空用默认 ~/projects/trss-yunzai/config/config/other.yaml |
| `backup_enabled` | `true` | 记忆库与状态文件自动备份 |
| `backup_dir` | - | 备份目录，留空落插件 data/backups |
| `backup_keep` | `7` | 记忆库备份保留几份 |
| `backup_interval_hours` | `24` | 记忆库备份间隔（小时） |
| `context_window_sec` | `1800` | 主模型上下文时间窗（秒）：这段时间内的发言都进 prompt，别再按固定条数截断话题 |
| `social_context_sec` | `1800` | 第一步判官（social_read）的上下文时间窗（秒），太短会让它把刚聊过的事再问一遍 |
| `enabled_private_users` | `[]` | 私聊白名单。留空 = 只有 owner_qq 能私聊。填了才对外，别填陌生号。 |
| `reply_max_chars` | `260` | 单条回复字数上限。超过就截断。跟 split_max_chars 不是一回事：那个是拆条，这个是防跑题。 |
| `tech_repeat_window_sec` | `600` | 技术话题复读窗口。这段时间内同一个技术标识符（型号/型号/参数）说过了就不再重复。 |
| `natural_song` | `true` | 自然语言点歌。开着就能直接说「我想听晴天」，不用敲指令。 |
| `natural_tattle` | `true` | 主动上报开关。关掉后模型照常判断，但不私聊上报主人。 |
| `order_guard_enabled` | `true` | 群秩序巡检总开关。广告/刷屏/辱骂的主动上报，判断在提示词里，这里只是总闸。 |
| `order_guard_cooldown_sec` | `600` | 同群上报冷却。同一个群多少秒内只打扰主人一次。 |
| `tattle_watch_groups` | `[]` | 主动上报的群白名单。留空 = 一个群都不上报。只有这里列出的群才会因为违规私聊主人。 |
| `vision_provider_id` | `gemini-vision` | 框架里的视觉模型（在 AstrBot 模型页配的 provider id）。留空或框架里找不到这个 id 时，自动回落插件自带的直连通道 |
| `vision_use_framework_provider` | `true` | 优先用框架视觉模型识图。关掉则只走插件自带的直连 Gemini 通道 |
| `hearing_enabled` | `true` | 听懂语音（把语音转成文字）。关掉后别人发语音鱼就当没内容，回到「听不到语音」的老行为 |
| `hearing_model` | `gemini-3.5-flash-lite` | 语音转文字用的模型 |
| `hearing_timeout_sec` | `30` | 单条语音转写超时（秒） |
| `hearing_prompt` | - | 转写提示词（留空用内置） |
| `meme_gen_enabled` | `true` | 自己画带字的表情包（[画图:模板:文字]） |
| `meme_gen_timeout_sec` | `25` | 单张画图超时（秒） |
| `patrol_judge_gap_sec` | `45` | 同一群两次巡检判定的最小间隔（秒），防刷屏时疯狂调模型 |
| `patrol_judge_timeout_sec` | `12` | 巡检判定单次超时（秒） |
| `tattle_intent_timeout_sec` | `10` | 「是不是在让我去告状」判定超时（秒） |
| `follow_reply_cap` | `3` | 追问窗口内，这个时长里最多由它再发几条（0=不限），防碎嘴 |
| `follow_reply_window_sec` | `180` | 上面那个条数的统计窗口（秒） |
| `video_download_enabled` | `true` | 视频解析：贴 B 站链接直接发视频。关掉后只发图文卡片，不下载视频文件。 |
| `video_http_host` | - | 视频中转地址（本机内网 IP）。NapCat 跑在别的机器上，读不到本机文件路径，下载好的片子得经本机临时 HTTP 喂给它。留空自动探测本机内网 IP。 |
| `video_max_mb` | `80` | 单条视频体积上限（MB）。超过就先降画质，降到 360p 还超就只发卡片。 |
| `video_max_minutes` | `30` | 单条视频时长上限（分钟）。超过就直接只发卡片，别让两小时直播回放把磁盘和群都撑爆。 |
