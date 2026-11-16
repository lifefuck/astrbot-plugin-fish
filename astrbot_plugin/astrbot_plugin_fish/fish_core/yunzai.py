# -*- coding: utf-8 -*-
"""云崽（TRSS-Yunzai）白名单联动。

老仓库 `/off` 会把群从云崽的 `whiteGroup` 里摘掉，否则群里还有别的插件在动，
「全关」就只是关了我自己。这段是从 `bot.py::sync_yunzai_whitelist` 原样搬的，
只把路径改成参数、加在保存失败时不破坏原文件。

注意：这是别人插件的配置文件，改写必须同时满足三件事——
1. 原子写（临时文件 + os.replace），写到一半崩掉整个 other.yaml 会变空，云崽直接起不来；
2. 写之前用 yaml.safe_load 校验一遍，格式不对就放弃写入，宁可不联动也不能写坏；
3. 不认识的条目原样保留。以前只认 `- 123456`，白名单里写成 `- 1234` 或
   `- ${group_id}` 的行会在一次 /off 之后被静默删掉。
"""

import logging
import os
import re

logger = logging.getLogger("astrbot")

DEFAULT_YUNZAI_YAML = os.path.expanduser("~/projects/trss-yunzai/config/config/other.yaml")

# `- 123456` / `- "123456"` / `- 123456 # 二群` 都算合法群号行
_NUM_ITEM = re.compile(r"^(\s*)-\s*[\"']?(\d{5,12})[\"']?\s*(?:#.*)?$")
# `whiteGroup: []` / `whiteGroup: [1, 2]  # 注释`
_INLINE = re.compile(r"^(\s*whiteGroup\s*:\s*)\[(.*?)\]\s*(#.*)?$")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _validate_or_raise(tmp: str) -> None:
    """写盘前先确认这份 YAML 能被解析。校验不过就删掉临时文件并抛错。"""
    try:
        import yaml
    except ImportError:
        return  # 环境里没有 pyyaml 就跳过校验，云崽自己装了自己会报
    try:
        with open(tmp, encoding="utf-8") as f:
            yaml.safe_load(f)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(f"生成的 YAML 不合法，已放弃写入（原文件没动）：{e}")


def _parse(path: str):
    """拆出 whiteGroup 段。

    返回 (lines, header_i, header_line, entries, item_indent, end_i)，
    entries 是 [(group_id 或 None, 原始行或 None)]——None 表示这行得重新生成，
    原始行不为 None 时必须原样写回（那是我们不认识的条目）。
    """
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    header_i = -1
    for i, line in enumerate(lines):
        if line.lstrip().startswith("whiteGroup:"):
            header_i = i
            break
    if header_i == -1:
        return lines, -1, "", [], 0, len(lines)

    header = lines[header_i]
    header_indent = _indent_of(header)

    # 段尾：遇到顶格且不是列表项的行就结束
    end_i, item_indent = len(lines), None
    for j in range(header_i + 1, len(lines)):
        s = lines[j].strip()
        if not s or s.startswith("#"):
            continue
        ind = _indent_of(lines[j])
        if s.startswith("-"):
            if item_indent is None or ind < item_indent:
                item_indent = ind
            continue
        if ind <= header_indent:
            end_i = j
            break

    entries = []
    m = _INLINE.match(header.rstrip("\n"))
    if m:
        # 内联列表必须先展开成块：`whiteGroup: []` 后面直接插 `  - 123` 是非法 YAML
        for part in re.split(r"[,，]", m.group(2)):
            part = part.strip().strip("\"'")
            if not part:
                continue
            entries.append((int(part) if part.isdigit() else None, None))
        comment = (m.group(3) or "").strip()
        prefix = m.group(1).rstrip()
        header = f"{prefix}  {comment}\n" if comment else f"{prefix}\n"
    else:
        for line in lines[header_i + 1:end_i]:
            s = line.strip()
            if not s or s.startswith("#") or not s.startswith("-"):
                continue
            mm = _NUM_ITEM.match(line.rstrip("\n"))
            if mm:
                entries.append((int(mm.group(2)), line))
            else:
                # 看不懂的条目照抄回去，绝不因为「不匹配群号格式」就删掉别人写的配置
                entries.append((None, line if line.endswith("\n") else line + "\n"))

    indent = item_indent if item_indent is not None else header_indent + 2
    return lines, header_i, header, entries, indent, end_i


def _gids(entries):
    return sorted(g for g, _ in entries if g is not None)


def sync_whitelist(path: str, group_id: int, enable: bool) -> bool:
    """把 group_id 加进/移出 whiteGroup。返回有没有真的写盘。"""
    path = os.path.expanduser(path or DEFAULT_YUNZAI_YAML)
    if not os.path.exists(path):
        logger.info("[云崽联动] 没找到 %s，跳过（不装云崽就用不上这条）", path)
        return False
    try:
        lines, header_i, header, entries, item_indent, end_i = _parse(path)
        if header_i == -1:
            logger.warning("[云崽联动] %s 里没有 whiteGroup 段，不动它", path)
            return False

        before = _gids(entries)
        if enable:
            if group_id not in before:
                entries.append((group_id, None))
        else:
            entries = [(g, raw) for g, raw in entries if g != group_id]

        # 状态本来就对就别写：这是别人插件的配置文件，
        # 每次 /off 都重写一遍等于天天给别人制造 diff 和写坏的机会
        if _gids(entries) == before:
            logger.info("[云崽联动] 群 %s 白名单已经是 %s，不用动", group_id, enable)
            return False

        body = []
        for gid, raw in entries:
            if raw is not None:
                body.append(raw)
            else:
                body.append(f"{' ' * item_indent}- {gid}\n")
        if not body:
            # 全删光了要写回 `whiteGroup:`，不能留一个空段头（否则和后面的键粘连）
            header = header.rstrip("\n") + "\n"

        out = lines[:header_i] + [header] + body + lines[end_i:]
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(out)
            f.flush()
            os.fsync(f.fileno())
        _validate_or_raise(tmp)
        # 保住原文件权限：临时文件按 umask 建，直接 replace 会把 600 变成 644
        try:
            os.chmod(tmp, os.stat(path).st_mode)
        except OSError:
            pass
        os.replace(tmp, path)
        logger.info("[云崽联动] 群 %s 白名单 -> %s（当前 %s）", group_id, enable, _gids(entries))
        return True
    except Exception as e:
        logger.error("[云崽联动] 同步 %s 失败：%s", path, e)
        return False


def in_whitelist(path: str, group_id: int) -> bool:
    """这个群当前在不在云崽 whiteGroup 里。

    /off 之前必须先问一句：本来就不在白名单的群，/on 时再 sync(True) 就是
    凭空往别人的配置里塞一个群号——注入测试的假群 999999 就是这么被写进去的。
    读不出来按「不在」处理，宁可少加一个，也不乱改别人的文件。
    """
    path = os.path.expanduser(path or DEFAULT_YUNZAI_YAML)
    try:
        _lines, header_i, _header, entries, _ind, _end = _parse(path)
    except Exception:
        return False
    if header_i == -1:
        return False
    return group_id in _gids(entries)
