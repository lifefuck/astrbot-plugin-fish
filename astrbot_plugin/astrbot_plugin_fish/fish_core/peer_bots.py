# -*- coding: utf-8 -*-
"""认出群里别的机器人：名单 + 昵称特征，两个都得有。

OneBot 协议不告诉你是谁发的（没有 is_bot 字段），只能猜：
- 名单：data/peer_bots.json，一个 QQ 数组，随手手改；文件不在就建一个空数组，
  并在日志里打出路径——不然「为什么没生效」要翻半天。
- 昵称特征：机器人/助手/bot 这类词。宁可漏判也别误判：把真人当成机器人，
  等于这个人的话永远不被接。
"""

from __future__ import annotations

import json
import os

NAME_HINTS = ("机器人", "bot", "助手", "管家", "小助手", "bot酱")


def load(path: str, logger=None) -> set[str]:
    """返回 QQ 字符串集合。文件缺失就创建 []，格式坏了就当空名单继续跑。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):  # 手滑写成 {"qqs": [...]} 也认
            data = data.get("qqs") or data.get("bots") or []
        return {str(q).strip() for q in data if str(q).strip()}
    except FileNotFoundError:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
            if logger:
                logger.info("[fish] 没找到机器人名单，已建空数组：%s（把别的机器人 QQ 填进去）", path)
        except Exception as e:
            if logger:
                logger.warning("[fish] 建机器人名单失败 %s: %s", path, e)
        return set()
    except Exception as e:
        if logger:
            logger.warning("[fish] 机器人名单读取失败 %s: %s", path, e)
        return set()


def is_peer_bot(uid: str, nick: str, peers: set[str], extra_hints=()) -> bool:
    if str(uid) in peers:
        return True
    n = (nick or "").lower()
    if not n:
        return False
    return any(h.lower() in n for h in tuple(NAME_HINTS) + tuple(extra_hints or ()))
