# -*- coding: utf-8 -*-
"""群内玩法：斗图反杀。

被 @ 且带了图，就甩一句回怼 + 一张自己的图。不挂人设，也绝不调模型——
这个场合要的就是快，模型还在排队的时候群里早就笑完了。

和旧实现的区别：旧版直接 await bot.send_group_message / send_meme_message，
把「怎么发」和「发什么」焊死在一起，搬到 AstrBot 就没法用（那边是消息链、
是 event.send，不是 ws + 裸 dict）。现在这里只做决策、返回一个动作字典，
发送交给调用方：

    action = meme_battle(text=..., image_url=..., meme_catalog=meme_catalog,
                         last_battle_ts=本群上次斗图时间戳)
    if action:
        # 1. 接管这条消息，别再走正常回复
        # 2. 先发 action["text"]（@ action["at_user_id"]）
        # 3. 等 action["delay_sec"] 秒，再发 Image.fromBase64(action["payload"])
        # 4. 把 action["battle_ts"] 存进本群状态，下次当 last_battle_ts 传进来
    else:
        # 没接管：没图 / 刚斗过 / 图库空，照常走后面的逻辑

节流状态（旧版那个 _last_battle 全局 dict）故意不放在这里：纯函数一旦自己
藏状态，就成了难测的隐式单例，多进程/多份插件实例还会各算各的。
"""

from __future__ import annotations

import random
import time
from typing import Optional

# 斗图回怼的本地语料。故意短、故意欠揍，长句在这种一来一回里反而尴尬。
BATTLE_LINES = (
    "图不错，我的了",
    "就这？",
    "存了，下次拿来怼你",
    "你这图有点年份了",
    "接住",
    "翻遍库存找出这张",
    "看把你急的",
)

# 文字和图片之间的停顿。和人一样，甩完嘴炮要顿一下再丢图。
DEFAULT_DELAY_SEC = 0.4

# 同一群多久内只反一次。有人连着刷图时，不留这个间隔群就被表情包填满了。
DEFAULT_COOLDOWN_SEC = 30.0

_RNG = random.Random()


def meme_battle(
    *,
    text: str = "",
    image_url: Optional[str] = None,
    meme_catalog,
    rng: Optional[random.Random] = None,
    now: Optional[float] = None,
    last_battle_ts: float = 0.0,
    cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
    group_id=None,
    user_id=None,
    lines=None,
) -> Optional[dict]:
    """判断这一条要不要斗图反杀，要就返回动作字典，不要就 None。

    参数：
      text           群友这条消息的正文，用来撞表情包关键词
      image_url      这条消息里的图片地址；没有图就不接管（旧版其实没检查，
                     因为它只会在「@ + 带图」的分支里被调用，这里把前提做成显式判断）
      meme_catalog   fish_core/meme_catalog 模块本身（注入而不是 import，测试才好替身）
      rng            随机源，测试时给个固定种子
      now            当前时间戳，注入以便测冷却
      last_battle_ts 本群上一次斗图成功的时间戳，0 表示没斗过
      cooldown_sec   冷却秒数，<=0 表示不限
      group_id / user_id 原样带进动作字典，方便调用方回填，不参与判定

    返回的字典：
      {
        "type": "meme",
        "payload": "<base64 字符串>",
        "payload_kind": "base64",
        "text": "反杀用的那句话",
        "at_user_id": <要 @ 的人>,
        "group_id": <群号>,
        "delay_sec": 0.4,
        "battle_ts": <本次时间戳，调用方存下来当 last_battle_ts>,
      }
    """
    rng = rng or _RNG
    now = time.time() if now is None else now

    # 1. 没图就没什么可斗的。先判这个再判冷却，免得「有人发纯文字 @」也把冷却烧掉
    if not image_url:
        return None

    # 2. 冷却：同一群 30 秒只反一次
    if cooldown_sec and cooldown_sec > 0 and now - (last_battle_ts or 0.0) < cooldown_sec:
        return None

    # 3. 挑图。图库没加载 / 全被体积过滤掉 / 模块根本没注入，都返回 None，
    #    这时不接管，也别写 battle_ts——不然一次空池就把这群的冷却白白占掉
    if meme_catalog is None:
        return None
    b64 = meme_catalog.pick_b64(None, text or "")
    if not b64:
        return None

    return {
        "type": "meme",
        "payload": b64,
        "payload_kind": "base64",
        "text": rng.choice(tuple(lines) if lines else BATTLE_LINES),
        "at_user_id": user_id,
        "group_id": group_id,
        "delay_sec": DEFAULT_DELAY_SEC,
        "battle_ts": now,
    }
