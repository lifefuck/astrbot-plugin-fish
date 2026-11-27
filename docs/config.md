# 配置参考

本文件由 `scripts/gen_config_doc.py` 从 `_conf_schema.json` 生成，**不要手改**。
改配置项后跑一次生成脚本；CI 会检查它和 schema 是否一致。

面板里每一项都带 `hint`，这里只做速查。默认值列里的 `-` 表示默认空字符串。

## 核心配置

| 配置项 | 默认 | 说明 |
| :--- | :--- | :--- |
| `enabled` | `true` | 总开关。关掉后插件完全不介入，群里恢复 AstrBot 原生行为（只有 @ 和唤醒前缀会回）。 |
| `enabled_groups` | `[]` | 生效群白名单。留空 = 所有群都不生效（安全默认）。填群号才上线，填 ["*"] 对所有群生效。 |
| `enabled_private_users` | `[]` | 私聊白名单。留空 = 只有 owner_qq 能私聊。填了才对外，别填陌生号。 |
| `owner_qq` | - | 主人 QQ，/off /on /memes 除群管外也放行；留空则只认群管 |
| `owner_favor_lock` | `100` | 主人好感度锁定值。主人在群里的好感分不随风波动，固定按这个值算。 |
| `bot_nickname` | `小鲸鱼` | QQ 上的昵称。只在 prompt 里锚定身份用，避免模型把自己的名字当成别人。 |
| `self_nickname` | - | 合并转发卡片里的落款名。检索答案、长文用合并聊天记录发时，卡片头上显示的名字。留空 = 跟上面「QQ 上的昵称」一致。 |
| `wake_prefix` | `ab!` | AstrBot 唤醒前缀（仅用于识别，不拦消息）。带这个前缀的消息完全交给 AstrBot 自己的 LLM 链路处理，小鲸鱼不插手。必须和 AstrBot 全局唤醒前缀一致。 |
| `reply_when_at` | `true` | 被 @ 必回 |
| `reply_private` | `true` | 私聊必回 |
| `cooldown_sec` | `5` | 被叫后的最短回复间隔(秒)。被@/被骂/唤醒也要过这条冷却，只是比追问窗口的更短。 |
| `chat_min_gap_sec` | `2` | 对话延续最小间隔（秒）。120 秒硬冷却只管「主动插嘴」；同一个人接着聊、直呼外号、主人开口都只受这个间隔限制。设 0 = 不限。 |
| `interject_desire` | `0.3` | 主动接话意愿。越高门槛越低。0.30 → 质量门槛 0.62（普通闲聊不接，提问/玩梗/点名才接）；0.60 → 门槛 0.40（公式算出来是 0.38，被下限夹到 0.40，也就是几乎每条都插一句）。 |
| `interject_hourly_cap` | `8` | 每小时主动插话上限。被 @ 回话不受这个上限约束。 |
| `interject_cooldown_sec` | `120` | 主动接话硬冷却（秒）。刚说完话这段时间内绝不再主动插嘴，防霸屏。 |
| `follow_window_sec` | `300` | 被叫过后免 @ 追问窗口（秒） |
| `follow_max_turns` | `2` | 追问窗口里最多连续免 @ 几次 |
| `follow_min_gap_sec` | `8` | 追问窗口内两次回复的最小间隔（秒）,防止一句一回刷屏 |
| `context_window_sec` | `7200` | 主模型上下文时间窗（秒）：这段时间内的发言都进 prompt，别再按固定条数截断话题 |
| `context_turns_max` | `96` | 上文最多保留条数。超过就一次砍掉四分之一。砍得越频繁命中率越低，48 条约二三十轮一次。 |
| `timeline_size` | `60` | 上下文条数上限（配合 context_window_sec 时间窗，先按时间筛再截条数） |
| `memory_recall_topk` | `4` | 记忆召回条数 |
| `memory_source_required` | `true` | 记忆必须带出处才允许主动提。关掉会退回老行为：没有原文锚点的旧记忆也会被主动引用。没事别关。 |
| `memory_min_confidence` | `0.5` | 主动引用记忆的最低置信度 |
| `memory_said_repeat_warn` | `2` | 同一件事对同一个人说过几次后先自查。到了这个次数，第三次开口前会先提醒自己别从头再讲一遍。 |
| `memory_conf_decay_days` | `30` | 低置信度记忆多少天没人印证就降一档 |
| `memory_prompt_budget` | `160` | 记忆块字数上限。条数之外再卡一道字数，否则出处和动作提示会把正文挤成配角。 |
| `prompt_budget_memory_max` | `2` | 拼进提示词的记忆条数上限。多了反而把人物的口气冲散。只留最相关的几条，超了按相关度丢。 |
| `prompt_budget_style_max` | `5` | 拼进提示词的风格样例上限（组）。样例是给人学口气的，默认 5 组：三组撑不起语感，模型会退回自己的模板腔。硬塞十组，模型会开始抄样例的字。 |
| `prompt_budget_hint_max` | `2` | 拼进提示词的提醒类块数上限。吵架/跨群/社交解读/防复读/表情包/百科这些提示加起来最多几条。超了按优先级丢，优先级：吵架>跨群>社交>防复读>表情包>百科。 |
| `empty_context_ask_short` | `true` | 上下文太薄时先问一句短的。时间线为空、或只有一条且跟当前这句接不上时不调主模型：被@就先问一句短的让人补，没人叫就直接不开口。关掉等于一律不开口。 |
| `vision_enabled` | `true` | 群里发图时让 Gemini 看一眼。只在决定要回的时候才识图，不会为每张图烧配额。识图失败静默降级成纯文字。 |
| `vision_model` | `gemini-3.5-flash-lite` | 视觉主模型。默认 lite：实测 1~2 秒就能把图里的字逐字抄准；gemini-3.8-flash 在这台机器上常 25 秒超时，不适合群聊实时场景。 |
| `vision_fallback_models` | `["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]` | 视觉降级模型。只放 lite 系：实测 3.5/3.1-flash-lite、flash-lite-latest 都在 1~3 秒出描述；3.6/3.8-flash 同样一张图要 14~21 秒，配 12 秒总预算等于永远轮不上。 |
| `vision_max_images` | `2` | 单条消息最多看几张图 |
| `vision_timeout_sec` | `12` | 单模型超时（秒）。群聊等不起，超时立刻换下一个模型。 |
| `hearing_enabled` | `true` | 听懂语音（把语音转成文字）。关掉后别人发语音鱼就当没内容，回到「听不到语音」的老行为 |
| `meme_enabled` | `true` | 按情绪标签配表情包。模型写了 [MEME:情绪] 时附一张图；标签本身一定被剥掉，不会漏进群。 |
| `meme_min_gap_sec` | `180` | 两张表情包之间的最小间隔（秒）。防止连着几轮都在甩图。 |
| `pixiv_enabled` | `true` | 按标签发图（模型写 [看图:标签] 时去 pixiv 捞一张，只发全年龄） |
| `bot_ban_guard` | `true` | 机器人被禁言时不调模型（照样收得到消息，白烧 token） |
| `density_guard` | `true` | 刷屏闸：两分钟内主动说过 N 条就不再主动搭腔（被 @ 不受限） |
| `split_enabled` | `true` | 长回复拆条。超过字数阈值就拆成 2~3 条隔 0.6~1.5 秒补发，像人连着敲。括号和表情标签不会被切断。 |
| `split_max_chars` | `45` | 拆条字数阈值。单条超过这么多字就拆成两条。真人一条消息通常十几二十字，超过这个数就该换行了。0 或负数 = 永不拆。 |
| `reply_max_chars` | `260` | 单条回复字数上限。超过就截断。跟 split_max_chars 不是一回事：那个是拆条，这个是防跑题。 |
| `humanize_short_cut` | `false` | 单字硬切（好/行/行吧整条删）。默认关：fish 里「好」常是一条正经回复，开了会变成对 @ 它的人装死 |
| `humanize_fiction` | `false` | 小说腔整句删（嘴角勾起弧度等）。群聊基本不出现，先关着 |
| `human_comma_split` | `true` | 短句按逗号拆两条。26 字内按第一个逗号拆，0.7 概率，和长句拆条互补 |
| `ctx_keep_turns` | `30` | 上文最多带多少轮。从 user 那条切开，不留孤儿助手话 |
| `aiflavour_enabled` | `true` | AI 味动态拦截 |
| `aiflavour_mode` | `strip` | AI 味拦截模式。strip=剥掉 \| shadow=只记日志 \| block=整条拦 |
| `aiflavour_learn_min` | `3` | 词根升级阈值。同群累计命中几次才开始剥 |
| `aiflavour_deactivate_days` | `7` | 词根自动降级天数 |
| `aiflavour_short_window` | `180` | 会话刹车窗口（秒） |
| `huoren_typo_enabled` | `true` | 毛边：偶尔打错一个字（一次最多一个，带自我更正） |
| `huoren_typo_rate` | `0.06` | 错别字概率，0.06≈三十条来一条 |
| `huoren_correction_rate` | `0.5` | 打错字后补一句「打错了，是X」的概率 |
| `huoren_mood` | `true` | 心流：energy/warmth 按会话积累并压成一句提示词 |
| `yunzai_other_yaml` | `~/projects/trss-yunzai/config/config/other.yaml` | 云崽 other.yaml 路径。/off /on 要同步 whiteGroup 的那个文件。留空 = 不同步，绝不回退到别人的默认路径（以前缺省会去改另一个服务的配置）。 |
| `yunzai_sync` | `true` | /off 时同步云崽白名单。只在你确实同时跑云崽、且上面路径填对了的时候才开。 |

## 高级分组：视觉/语音进阶（`advanced_vision`）

换 provider、换模型、代理和 key 来源，一般不用动。这一组都是调优项，改坏了会安静地变难用，不确定就别动。

| 配置项 | 默认 | 说明 |
| :--- | :--- | :--- |
| `vision_wb_enable` | `true` | 识图先走 WB 池（本地 3425 网关）。Gemini 常年限流，群里发图就瞎。WB 池实测 2.6~4.3s 出描述、连打 6 发不限流，所以放第一位，Gemini 只当兜底。 |
| `vision_wb_base` | `http://127.0.0.1:3425/v1` | WB 网关地址 |
| `vision_wb_key` | - | WB 网关密钥（本地网关留空即可） |
| `vision_wb_models` | `["wb/glm-5v-turbo", "wb/minimax-m3", "google/models/gemini-flash-lite-latest"]` | WB 识图模型顺序。glm-5v-turbo 是视觉专用模型，读数字最准；后面两个是实测过的备胎（同一张截图关键词 8/8 全对、3.4~3.5s）。别用 minimax-m3.1-flash，它只吐英文思考碎片、等于没有备胎；也别用 glm-5.1/5.2/5.3，会把 7392 念成 7.392。 |
| `vision_wb_timeout_sec` | `16` | 单个识图模型超时（秒）。实测成功最慢 9.6s，钉死 10s 会让长尾那几次直接 TimeoutError、整条链路空手而归。真正的天花板是 vision_total_budget_sec。 |
| `vision_total_budget_sec` | `20` | 识图总预算（秒）。一张图最多等这么久，超了就按没有图继续回。 |
| `vision_proxy` | `http://127.0.0.1:7890` | 访问 Google 的代理。留空 = 直连。国内机器基本都得填。 |
| `vision_api_key` | - | Gemini 密钥（可选）。优先级：环境变量 > vision_key_env_file > 这里。建议留空，用 env 文件。 |
| `vision_key_env_file` | `~/.config/qq-deepseek-bot.env` | 读取密钥的 env 文件。按 KEY=VALUE 解析，只取 GEMINI_API_KEY / GOOGLE_API_KEY / VISION_*。 |
| `vision_provider_id` | `gemini-vision` | 框架里的视觉模型（在 AstrBot 模型页配的 provider id）。留空或框架里找不到这个 id 时，自动回落插件自带的直连通道 |
| `vision_use_framework_provider` | `true` | 优先用框架视觉模型识图。关掉则只走插件自带的直连 Gemini 通道 |
| `hearing_model` | `gemini-3.5-flash-lite` | 语音转文字用的模型 |
| `hearing_timeout_sec` | `30` | 单条语音转写超时（秒） |
| `hearing_prompt` | - | 转写提示词（留空用内置） |

## 高级分组：表情包细节（`advanced_meme`）

池子大小、轮换、出图、防连发。这一组都是调优项，改坏了会安静地变难用，不确定就别动。

| 配置项 | 默认 | 说明 |
| :--- | :--- | :--- |
| `meme_gate_enabled` | `true` | 斗图闸。同一话题一回合最多一张：刚发过、群友刚发图、标签和上次相同都拦。 |
| `meme_follow_gap_sec` | `60` | 对方发图后多久内不跟图（秒）。跟图就成斗图刷屏了。 |
| `meme_battle_enabled` | `true` | 有人 @ 它还甩图时斗图反杀 |
| `meme_pool_size` | `30` | 热梗池每轮注入几条 |
| `meme_rotation_sec` | `1800` | 热梗池多久换一轮（秒） |
| `meme_gen_enabled` | `true` | 自己画带字的表情包（[画图:模板:文字]） |
| `meme_gen_timeout_sec` | `25` | 单张画图超时（秒） |

## 高级分组：对话细节（`advanced_chat`）

拆条、判官、社交读、影子模式。这一组都是调优项，改坏了会安静地变难用，不确定就别动。

| 配置项 | 默认 | 说明 |
| :--- | :--- | :--- |
| `split_max_parts` | `3` | 最多拆成几条。超出的内容并进最后一条，宁可长一点也不刷屏。 |
| `split_gap_min` | `0.6` | 拆条最小间隔（秒） |
| `split_gap_max` | `1.5` | 拆条最大间隔（秒） |
| `style_learning` | `true` | 在线学习群表达样例。把群里相邻两条消息凑成「有人说→别人怎么接」，进 data/style_fewshot.json。 |
| `debug_dump_prompt` | `false` | 把每次拼好的 prompt 落盘。写到 data/last_prompt.txt，用于核对心境/记忆提示是否被注入。排查完记得关。 |
| `command_prefixes` | `["#", "*", "%", "~"]` | 其他插件的指令前缀。以这些字符开头的群消息（云崽面板指令）完全不介入。 |
| `social_read_enabled` | `true` | 两步生成：先判该不该接。先小调用判「这帮人在聊什么、我该不该接」，判不接就不调主模型。 |
| `social_read_provider` | - | judge 用的 provider id。留空 = 当前会话默认模型。填了就用指定 provider 省主模型额度。 |
| `social_read_timeout_sec` | `8` | judge 超时（秒）。超时/报错一律按「能接」放行，绝不因此把回复吞掉。 |
| `social_context_sec` | `1800` | 第一步判官（social_read）的上下文时间窗（秒），太短会让它把刚聊过的事再问一遍 |
| `judge_inline` | `true` | 判官并进主请求。开：不再单独发一次判官小请求，改由主模型判断「该不该接」，不想接就内部吐 [PASS]。省 3~5 秒，且少一个永远命不中缓存的请求。被 @ 和追问窗口不受影响，必回。 |
| `judge_skip_on_at` | `true` | 被 @ 时跳过判官。开：点名叫你时不再先花一次判官调用（实测中位 3.1 秒）问「该不该接」——反正必须回。代价是失去「这句在说 X」那行释义。 |
| `multi_turn_contexts` | `true` | 多轮上下文（缓存友好）。开：群聊记录按 messages 追加传递，前缀稳定，缓存命中率显著提高；关：退回旧版「每轮重拼整段时间线」。 |
| `natural_song` | `true` | 自然语言点歌。开着就能直接说「我想听晴天」，不用敲指令。 |
| `natural_tattle` | `true` | 主动上报开关。关掉后模型照常判断，但不私聊上报主人。 |
| `tech_repeat_window_sec` | `600` | 技术话题复读窗口。这段时间内同一个技术标识符（型号/型号/参数）说过了就不再重复。 |
| `density_window_sec` | `120` | 刷屏闸的统计窗口（秒） |
| `density_limit` | `2` | 窗口内最多主动说几条 |
| `speak_decision_shadow` | `false` | 开口判定影子模式。打开后每条消息都会用 fish_core.speak_decision 重新判一次，只写日志不改行为，用来灰度对比新旧判定。 |
| `speak_decision_shadow_groups` | `[]` | 影子模式灰度群。只对这些群号记影子日志；留空=所有群。 |
| `judge_broken_policy` | `skip` | 判官不可用时的策略。skip=不接（默认，没有依据就别乱说）；reply=照常接。 |
| `atoms_distill_timeout_sec` | `6` | 印象提炼超时（秒）。从群友的话里提炼长期印象时的等待上限；超时就跳过，不拖慢回复。 |
| `poke_back` | `true` | 被戳时回戳。有人戳它，先秒戳回去再回话。同一个 3 秒内只回戳一次，防止互戳死循环。 |
| `poke_model_timeout_sec` | `12` | 戳一戳走模型时的兜底超时（秒）。超过这个时间还没出话，就发一句本地兜底短句，保证这一戳有回应。 |
| `poke_fallback_pool` | `["手欠", "别戳", "干嘛啊", "戳啥", "再戳收费"]` | 戳一戳兜底短句池。只在模型超时/失败时用，平时一律走模型。5 句以内够了：池子再大也治不了复读，能让模型自己写才行。 |
| `group_memes_enabled` | `true` | 本群自己的梗：接话时优先用群里真实出现过的说法（由 scripts/mine_group_memes.py 从聊天记录统计） |
| `cold_atmosphere_enabled` | `true` | 冷场补气氛：安静久了先垫表情包、再拿本群老梗递话头 |
| `cold_field_gap_sec` | `900` | 上一条人话过去多少秒算冷场（默认 15 分钟） |
| `cold_meme_first` | `true` | 冷场时模型没写表情标签也先垫一张没攻击性的图（卖萌/睡觉/干饭） |
| `addressee_guard_enabled` | `true` | 收件人判定总开关：判断这句话到底是不是说给鱼的（关掉就退回只看判定链） |
| `addressee_hard_threshold` | `70` | 多少分以上认定「说给鱼」：被@/引用我=100，点名=70，追问/代词=60，鱼刚说过=30 |
| `addressee_gray_threshold` | `40` | 灰区下限：分数落在灰区以上但没到阈值时，交给判官裁决——判官过线就放行，没过线默认不是 |
| `addressee_thread_window_sec` | `180` | A↔B 两个人聊得正热的线程，多少秒没新消息算散场（线程里鱼是旁观者，不抢话） |
| `addressee_topic_gap_sec` | `60` | 鱼说过话之后多少秒内算同一段话口（弱信号，只值 30 分，单凭它不够阈值） |
| `addressee_alias_enabled` | `true` | 从真实 At 昵称和群名片学别名（只学不猜，不从聊天里编外号） |
| `topic_hint_enabled` | `true` | 把判官读出的「这句在说啥」当释义注入表达层（只给释义不给接法）；发现闲聊被带成解说腔就关掉 |
| `scheduler_default_silent` | `true` | 默认不说。总闸：没有 @ / 引用 / 追问 / 话头卡住 / 明确求助就闭嘴。关掉退回「判官给分就接」的旧口径。 |
| `rate_guard_enabled` | `true` | 发言率闸。窗口内鱼的发言量不超过群友人均，且不许连着说两句没人接。 |
| `rate_window_sec` | `600` | 发言率窗口（秒）。和条数上限一起用，先到者为准。 |
| `rate_window_msgs` | `50` | 发言率窗口条数。窗口内最多看多少条，防止刷屏把统计拉爆。 |
| `proactive_gap_sec` | `90` | 主动发言间隔（秒）。主动接话两次之间至少隔这么久；被 @ / 追问不受此限。 |
| `typing_delay_enabled` | `true` | 打字延迟。按字数模拟打字时间（1~6 秒），期间群里换了话题就放弃这条。 |
| `typing_max_sec` | `3.0` | 打字延迟上限（秒）。长段回复也不要让人等太久。模型自己就要四五秒，这里再拖三四秒，一句二十字要等快十秒。 |
| `draft_ttl_sec` | `25` | 草稿存活（秒）。草稿超过这个时间还没发出去就作废，不再补发。 |
| `tease_guard_enabled` | `true` | 调侃许可。按人按群分三级：无许可只中性、低许可能自嘲、高许可能挤对行为。外貌/性别/家庭/工作能力永远是禁区。 |
| `tease_high_favor` | `80` | 高许可好感线。好感到这个分数直接给高许可（可以笑着挤对对方的行为）。 |
| `tease_low_favor` | `60` | 低许可好感线。到这个分数给低许可（可以拿自己开涮，不拿对方说事）。 |
| `tease_ban_days` | `30` | 拒绝后静默天数。被明确拒绝（别开这种玩笑/不喜欢这样）后多少天不许再试。道歉、投喂都不缩短；累计三次锁死。 |
| `self_tease_per_hour` | `1` | 自嘲每小时次数。每小时最多自嘲几次。超了这轮就不给自嘲许可，并做出口指纹去重，防模板化。 |
| `presence_enabled` | `true` | 在场模式。对不需要回答但值得有反应的消息（在笑、摆烂、抛梗），只丢一张表情或一个短词，不调模型。 |
| `presence_gap_sec` | `120` | 在场间隔（秒）。两次在场之间至少隔多久。 |
| `presence_per_10min` | `2` | 在场次数上限。10 分钟内最多在场几次。配额说了不算说话的配额，是单独一池。 |

## 高级分组：风控与告状（`advanced_safety`）

吵嘴、骂人、点单拦截、巡逻判官。这一组都是调优项，改坏了会安静地变难用，不确定就别动。

| 配置项 | 默认 | 说明 |
| :--- | :--- | :--- |
| `quarrel_enabled` | `true` | 被骂了还嘴。只有点名骂它、或骂到主人头上才触发；别人互骂一律不动。判定在 fish_core/quarrel.py，不占提示词。 |
| `insult_actions_enabled` | `true` | 让鱼去骂人。群友说「骂一下谁」「群嘲」时真的 @ 到人头上；不骂群管和主人。 |
| `insult_cooldown_sec` | `60` | 骂人间隔（秒）。同一个群两次执行的最小间隔，防止有人拿它刷屏。 |
| `insult_max_targets` | `3` | 群嘲最多点几个人。一次无差别攻击最多 @ 的人数。 |
| `order_guard_enabled` | `true` | 群秩序巡检总开关。广告/刷屏/辱骂的主动上报，判断在提示词里，这里只是总闸。 |
| `order_guard_cooldown_sec` | `600` | 同群上报冷却。同一个群多少秒内只打扰主人一次。 |
| `tattle_watch_groups` | `[]` | 主动上报的群白名单。留空 = 一个群都不上报。只有这里列出的群才会因为违规私聊主人。 |
| `tattle_intent_timeout_sec` | `10` | 「是不是在让我去告状」判定超时（秒） |
| `patrol_judge_gap_sec` | `45` | 同一群两次巡检判定的最小间隔（秒），防刷屏时疯狂调模型 |
| `patrol_judge_timeout_sec` | `12` | 巡检判定单次超时（秒） |
| `admin_actions_enabled` | `true` | 自然语言处置人：群管说「把某人禁言十分钟」「踢了他」会真执行 |
| `follow_reply_cap` | `3` | 追问窗口内，这个时长里最多由它再发几条（0=不限），防碎嘴 |
| `follow_reply_window_sec` | `180` | 上面那个条数的统计窗口（秒） |

## 高级分组：外部工具与数据（`advanced_tools`）

搜索/研究、云崽、备份、Pixiv 凭证、跨群、面板。这一组都是调优项，改坏了会安静地变难用，不确定就别动。

| 配置项 | 默认 | 说明 |
| :--- | :--- | :--- |
| `connectors_enabled` | `true` | 被问到出处/最新消息时才联网检索 |
| `research_enabled` | `true` | 被 @ 问到正经问题时，先搜网页、读正文，再回答。开了之后这类问题会先发一句「我去查下」，再回带出处的完整答案。闲聊完全不受影响，走原来的拆条和口癖。 |
| `research_max_rounds` | `4` | 检索最多几轮工具调用。每轮可以搜一次或读一个网页。4 轮够覆盖「查型号→查代号→读状态表」；调大只会更慢。 |
| `research_budget_sec` | `150` | 一次检索的总时长上限（秒）。到点就拿手上已有的材料收尾，不会把群挂在那儿等。 |
| `search_proxy` | - | 搜索用的 HTTP 代理。留空自动用 http://127.0.0.1:7890（DuckDuckGo 直连不通）。代理没开时自动退到百度。 |
| `enable_games` | `true` | 文字游戏：掷骰子、抛硬币、抽签 |
| `yunzai_sync` | `true` | /off 时把本群从云崽白名单摘掉，/on 加回来 |
| `yunzai_other_yaml` | - | 云崽 other.yaml 路径，留空用默认 ~/projects/trss-yunzai/config/config/other.yaml |
| `backup_enabled` | `true` | 记忆库与状态文件自动备份 |
| `backup_dir` | - | 备份目录，留空落插件 data/backups |
| `backup_keep` | `7` | 记忆库备份保留几份 |
| `backup_interval_hours` | `24` | 记忆库备份间隔（小时） |
| `peer_bots_path` | - | 其他机器人名单路径。data/peer_bots.json，一个 QQ 数组。留空 = 插件 data/peer_bots.json；文件不存在会自动建空数组。 |
| `peer_bots_name_hints` | `[]` | 机器人昵称特征（追加）。默认已含 机器人/bot/助手/管家，这里只补额外的。 |
| `cross_group_enabled` | `true` | 跨群记忆。把各群话题记成短摘要，拼 prompt 时允许提一句「别的群在聊」。 |
| `cross_group_recall` | `2` | 引用几条别的群话题。只引 3 小时内、去重后的短摘要。 |
| `cross_group_max_topics` | `60` | 话题库上限 |
| `panel_event_buffer` | `300` | 控制台事件流缓冲多少条 |
| `pixiv_cookie` | - | Pixiv Cookie（留空则自动借用流媒体解析插件里的那份）。需要包含 PHPSESSID；不填也能搜，但拿不到原图 |
| `pixiv_proxy` | `http://127.0.0.1:7890` | Pixiv 走哪个代理（国内必须） |
| `pixiv_timeout_sec` | `25` | 一次取图的总上限（秒）。超时就当没图，别卡住整条回复。 |
