"""斗图闸：表情包别连发、别跟图。

群友斗图的观感几乎全靠「一回合一张」：连着甩、跟着别人的图甩，立刻就变成
表情包轰炸，人味全没了。这里只做三条硬判定，不碰挑图逻辑。

抽成纯函数是有意为之——门限逻辑最容易在改阈值时悄悄坏掉，
放在 fish_core 里 selfcheck.py 才能不开 AstrBot 直接跑。

状态由调用方（插件里的 GroupState）持有，本模块不存任何东西。
"""

from __future__ import annotations

# 判定结果里带原因，方便日志里直接说清楚为什么这轮纯文字
OK = (True, "")


def decide(
    tag: str,
    now: float,
    *,
    last_meme_ts: float = 0.0,
    recent_tags: list[str] | None = None,
    incoming_image_ts: float = 0.0,
    enabled: bool = True,
    min_gap_sec: float = 300.0,
    follow_gap_sec: float = 90.0,
) -> tuple[bool, str]:
    """能不能在这轮再发一张表情包。

    参数全是显式的：判定只看「上一次发图多久前」「群友是不是刚发图」
    「上次用的标签」，不读配置、不读全局状态。
    """
    if not enabled:
        return True, "闸门关"
    # 时钟回拨 / 未来时间戳：now - ts 成负数会一直小于门限，
    # 一次 NTP 校正就能把表情包锁死几小时。异常时间戳一律当没记录
    if last_meme_ts > now:
        last_meme_ts = 0.0
    if incoming_image_ts > now:
        incoming_image_ts = 0.0
    if min_gap_sec > 0 and last_meme_ts and now - last_meme_ts < min_gap_sec:
        return False, f"距上一张表情包 {int(now - last_meme_ts)}s，不足 {int(min_gap_sec)}s"
    if follow_gap_sec > 0 and incoming_image_ts and now - incoming_image_ts < follow_gap_sec:
        # 群友刚发图就跟着发，观感上就是「我在跟他斗图」，实际只是碰巧想配图
        return False, "群友刚发了图，再甩图就成了斗图"
    tags = recent_tags or []
    # 同一标签只在一小时内算重复。以前没有时间窗，模型连着吐「卖萌」就永久不发图
    if tag and tags and tags[-1] == tag:
        if not last_meme_ts or now - last_meme_ts < 3600:
            return False, f"上一次也是「{tag}」，同一话题不重复"
    return OK
