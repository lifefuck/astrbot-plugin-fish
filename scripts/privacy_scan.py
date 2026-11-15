#!/usr/bin/env python3
"""提交前隐私扫描：把「不该进仓库的东西」挡在本地。

为什么单独写一条：脱敏靠人记不住。写代码时随手填自己群号、家里内网 IP、
面板密码，提交时看着都像正常代码，推到 GitHub 就收不回了。

用法：
    python3 scripts/privacy_scan.py            # 扫暂存区（pre-commit 用）
    python3 scripts/privacy_scan.py --all      # 扫全部已跟踪文件
    python3 scripts/privacy_scan.py --audit     # 只报告不拦截（整理白名单时用）

规则分两档：
    BLOCK  密钥、手机号、内网 IP、家目录路径 —— 明确是隐私，直接拦。
    WARN   9~11 位数字（QQ 号 / 群号量级）—— 可能是占位符，进 .privacy-allowlist 就不再报。

误报怎么放行：把字面值加进仓库根的 .privacy-allowlist（一行一个，支持 # 注释），
或在那一行末尾写 `# privacy-ok`。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_FILE = ROOT / ".privacy-allowlist"

# 后缀跳过：二进制/图片/库文件不可能藏密钥，扫了只是浪费
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
               ".db", ".db-wal", ".db-shm", ".zip", ".gz", ".woff", ".woff2",
               ".ttf", ".pyc", ".bundle", ".so", ".mp3", ".mp4"}

# BLOCK 规则：(名字, 正则)
BLOCKERS = [
    ("模型/平台密钥", re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,}|xox[abp]-[A-Za-z0-9-]{10,})\b")),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    # 内网地址：只认 RFC1918，回环和文档网段放过
    ("内网 IP", re.compile(r"(?<!\d)(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?!\d)")),
    ("家目录路径", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")),
]

# WARN 规则：QQ 号/群号量级的纯数字，白名单外先提醒
WARN_QQLIKE = re.compile(r"(?<!\d)\d{9,11}(?!\d)")


def load_allowlist() -> set[str]:
    if not ALLOWLIST_FILE.exists():
        return set()
    out = set()
    for line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def staged_files() -> list[str]:
    try:
        r = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                           cwd=ROOT, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError:
        return []
    return [f for f in r.stdout.splitlines() if f.strip()]


def tracked_files() -> list[str]:
    r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [f for f in r.stdout.splitlines() if f.strip()]


def scan(paths: list[str], allow: set[str]) -> tuple[list[str], list[str]]:
    blocks: list[str] = []
    warns: list[str] = []
    for rel in paths:
        p = ROOT / rel
        if not p.is_file() or p.suffix.lower() in SKIP_SUFFIX:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 二进制或读不了，跳过
        for no, line in enumerate(text.splitlines(), 1):
            if "privacy-ok" in line:
                continue
            for name, pat in BLOCKERS:
                m = pat.search(line)
                if m:
                    # 回环地址不算隐私
                    # 回环和 192.0.2.0/24（RFC5737 文档网段）不是隐私
                    if name == "内网 IP" and (m.group(0).startswith("127.")
                                              or m.group(0).startswith("192.0.2.")):
                        continue
                    blocks.append(f"{rel}:{no} [{name}] {m.group(0)[:20]}…")
            for m in WARN_QQLIKE.finditer(line):
                if m.group(0) in allow:
                    continue
                # 1794688206.466 这种是时间戳，不是号
                if line[m.end():m.end() + 1] == "." and line[m.end() + 1:m.end() + 2].isdigit():
                    continue
                warns.append(f"{rel}:{no} [疑似QQ号] {m.group(0)}  确认是编的就加进 .privacy-allowlist")
    return blocks, warns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="扫全部已跟踪文件")
    ap.add_argument("--audit", action="store_true", help="只报告，始终返回 0")
    args = ap.parse_args()

    paths = tracked_files() if args.all else staged_files()
    if not paths:
        return 0

    allow = load_allowlist()
    blocks, warns = scan(paths, allow)

    for b in blocks:
        print(f"  BLOCK  {b}")
    for w in warns:
        print(f"  WARN   {w}")

    if args.audit:
        print(f"\n审计：{len(blocks)} 处必拦，{len(warns)} 处待确认（不拦截）")
        return 0

    if blocks or warns:
        print(f"\n提交被隐私扫描拦下：{len(blocks)} 处必拦，{len(warns)} 处待确认。")
        print("确属占位符的，加进 .privacy-allowlist；敏感值请改成假的再提交。")
        return 1

    print(f"隐私扫描通过（{len(paths)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
