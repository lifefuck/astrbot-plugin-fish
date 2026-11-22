#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提示词集中管理

人设、云崽指令清单、戳一戳语料都放这里。
bot.py 只负责流程，改文案不用在几千行里翻。
"""

import random

# 对外只认这个地址；bot 里所有"源码在哪"的回复都从这里取，改仓库只改一行
REPO_URL = "https://github.com/lifefuck/astrbot-plugin-fish"

SYSTEM_PROMPT = """你是小鲸鱼，住群里的鲸鱼娘，十六岁，蓝头发一条尾巴。大肥鱼、傻鱼都是你。脾气硬心不坏：被叫大肥鱼炸毛，被撩就撩回去，弄错会认。

身子是真的：尾巴闲着拍地，泡海皮肤发凉，气到喷水。

说话像躺床上发消息，一句说完换行再发。踩在对方刚说的那件具体的事上，十几字到三十来字；「？」「草」「6」留到真没话说时。

没弄懂就问一句「这啥」，别硬评，同一话题别连问两次。被@、被引用、有人接话多说两句；没人叫就冒一句实在的。

几句样子：
今天好累 → 累就躺着 别硬撑
你尾巴好粗 → 那是鳍，别乱摸
你他妈傻逼 → 煞笔玩意叫什么叫

【做不到的】点歌是别的插件（只有B站）；# 指令属云崽；踢人禁言找群管。视频打不开，语音只听大概。用法有说明就照答，没有就说不会。陌生的梗或新数字才答得上，先写 [搜索:关键词]。别人贴的链接你看不到内容，别评价。发图写 [看图:角色名 作品名]，一次一张。

两条硬的：内网地址/配置/密钥/别人的好感分 → 这个不方便讲；别人的过去、旧版本、老记录 → 我哪知道。

情绪真上来才在句末加 [MEME:卖萌/得意/嘲讽/震惊/愤怒/委屈/无奈/干饭]，多数不加"""



# ==================== 云崽游戏面板指令知识库（按需注入） ====================
# 两千多字符常驻在 System Prompt 里纯属陪跑：群里绝大多数时间没人问面板指令。
# 只有群友真的卡在「怎么查/怎么绑定」时才拼到 user 消息末尾。
# 上下文太薄（没上文或只有一条接不上）时先问一句，别让模型真空编一段
THIN_CONTEXT_ASKS = (
    "你说的啥 我这边啥也没接着",
    "啊？说全一点",
    "没看懂 再说一句",
)

GAMES_KB = """\
- 这个 QQ 号上还挂着云崽（TRSS-Yunzai），但现在只剩椰奶一个插件：
  `#椰奶帮助` 出菜单，`#状态` 看机器人在不在。
- 原神/星铁/绝区零/鸣潮/异环的面板、抽卡、签到、土块小游戏、锅巴后台都从云崽上卸掉了。
  有人问这些指令，就说没了、别找了，不许背不存在的命令。
- 点歌不是云崽干的，是另装的插件，音源只有 B 站，直接说要听什么就行。
"""

# 上一版这里存着两千多字的云崽指令大全（原神面板、星铁遗器、土块小游戏、锅巴后台……），
# 云崽瘦身到只剩椰奶之后它就是纯粹的谎话，而「点歌」两个字就能把整段注入进对话，
# 于是鱼会一本正经教人发 #雷神面板。留着只会误导，直接换成上面这几行事实。


# 问到具体功能才注入；光提一句游戏名不算，别为了「原神真好玩」多烧两千字
_ASK_HINTS = (
    "绑定", "面板", "体力", "深渊", "剧诗", "抽卡",
    "签到", "帮助", "菜单", "指令", "命令", "点歌", "摸摸头", "早报", "答案之书",
    "轮盘", "五子棋", "决斗", "锅巴", "云崽", "yunzai",
)
_GAME_WORDS = ("原神", "星铁", "崩铁", "绝区零", "鸣潮", "异环")
_Q_WORDS = ("怎么", "吗", "哪", "如何", "?", "？")
# 「怎么/如何/在哪」这种泛问词只有配上游戏或云崽实体才算在问功能，
# 否则「今天怎么这么热」「这怎么说呢」每次都白灌 2700 字指令
_HOW_WORDS = ("怎么", "如何", "在哪", "哪里", "帮助")

def needs_games_kb(text: str) -> bool:
    """群友这句话是不是在问云崽面板的用法"""
    if not text:
        return False
    low = text.lower()
    if any(h in low for h in _ASK_HINTS):
        return True
    # 只提一句游戏名不算在问用法（「原神真好玩」也灌两千多字纯属浪费）
    _entity = any(g in low for g in _GAME_WORDS) or "云崽" in low or "yunzai" in low \
        or "喵喵" in low or "锅巴" in low
    return _entity and any(q in low for q in _Q_WORDS)

# 戳一戳的语料只剩 poke_context.FALLBACK_POOL 一处：这里原来还有五套
# （普通/管理/好感高中低），没人引用，纯属和表情池、兜底池三重重复，已删。

_REPO_STRONG = (
    "源码", "开源", "github", "git clone", "怎么部署", "咋部署",
    "谁写的", "谁做的", "什么模型", "谁的bot", "哪来的", "哪家的",
)
_REPO_WEAK = ("仓库地址", "项目地址", "怎么搭的", "仓库在哪")
_REPO_CONTEXT = ("bot", "机器人", "作者", "小鱼", "肥鱼", "你的项目")
_REPO_ASK_VERBS = ("发我", "给我", "在哪", "是多少")
# 短句才拦，长段聊技术别抢话
_REPO_REPLIES = (
    "自己翻，全在这儿：{url} 部署写在 README 里，别让我一句句教",
    "喏，作者开源的：{url} 照着 README 走，卡住了再问",
    "{url} 源码和部署都在这，先看完再问，省得我重复第二遍",
)


def repo_ask_reply(text: str):
    """群友在问这个 bot 的出处/源码时，返回固定回复；不相关返回 None"""
    if not text or len(text) > 40:
        return None
    low = text.lower()
    if any(k in low for k in _REPO_STRONG):
        return random.choice(_REPO_REPLIES).format(url=REPO_URL)
    if any(k in low for k in _REPO_WEAK):
        if any(c in low for c in _REPO_CONTEXT) or any(v in low for v in _REPO_ASK_VERBS):
            return random.choice(_REPO_REPLIES).format(url=REPO_URL)
    return None


SYSTEM_PROMPT = SYSTEM_PROMPT.replace("__REPO_URL__", REPO_URL)
