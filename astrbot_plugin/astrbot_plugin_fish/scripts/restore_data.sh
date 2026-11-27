#!/usr/bin/env bash
# 从 backup_data.sh 打出的包里恢复插件数据。
#
# 为什么需要专门一个脚本：包里的数据是为了「能读」而打的，不是为了「能原地解」。
# 直接 `tar -xzf pkg -C data/` 会覆盖正在运行的 fish_memory.db，而旁边的 -wal
# 还是旧库的，sqlite 下次打开就是 database disk image is malformed。
# 这里按顺序做：校验包 → 停服 → 备份现库 → 解到临时目录校验 → 换入 → 起服。
#
# 用法：
#   scripts/restore_data.sh <包路径>            # 演练：只校验，不动任何文件
#   scripts/restore_data.sh <包路径> --yes      # 真恢复（会重启 astrbot 用户服务）
#   scripts/restore_data.sh <包路径> --yes --no-restart
#
# 只恢复 data/ 与 config/；plugin/fish_core/data（t2i 模板）默认不动，
# 需要时加 --with-templates。
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$PLUGIN_DIR/data"
CONFIG_DIR="$(cd "$PLUGIN_DIR/../.." && pwd)/config"
SERVICE="${FISH_SERVICE:-astrbot}"

PKG="${1:-}"
[ -n "$PKG" ] && [ -f "$PKG" ] || { echo "用法: $0 <包路径> [--yes] [--no-restart] [--with-templates]"; exit 2; }
shift || true
DO_IT=0; RESTART=1; TEMPLATES=0
for arg in "$@"; do
  case "$arg" in
    --yes) DO_IT=1 ;;
    --no-restart) RESTART=0 ;;
    --with-templates) TEMPLATES=1 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== 1. 校验包 =="
gzip -t "$PKG"
# 同上：tar 管进 grep -q 会被 SIGPIPE 打成 141，先落文件再匹配
tar -tzf "$PKG" > "$WORK/list.txt"
grep -qx 'data/fish_memory.db' "$WORK/list.txt" || {
  echo "包里没有 data/fish_memory.db（可能是 2026-11 之前的平铺格式，先手工确认结构）"; exit 1; }
tar -xzf "$PKG" -C "$WORK"

echo "== 2. 校验库 =="
if command -v sqlite3 >/dev/null 2>&1; then
  RES="$(sqlite3 "$WORK/data/fish_memory.db" 'PRAGMA integrity_check;')"
  REC="$(sqlite3 "$WORK/data/fish_memory.db" 'select count(*) from fish_memory_records;' 2>/dev/null || echo '?')"
else
  RES="$("$PLUGIN_DIR"/../../.venv/bin/python - "$WORK/data/fish_memory.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute("PRAGMA integrity_check").fetchone()[0])
PY
)"
  REC="?"
fi
echo "  integrity_check=$RES  记忆条数=$REC"
[ "$RES" = "ok" ] || { echo "库自检不通过，拒绝恢复"; exit 1; }

echo "== 3. 计划 =="
echo "  包内容：$(tar -tzf "$PKG" | wc -l) 个成员"
echo "  将覆盖：$DATA_DIR/*.json 与 fish_memory.db"
echo "  将覆盖：$CONFIG_DIR/astrbot_plugin_fish_config.json"
if [ "$TEMPLATES" = 1 ]; then echo "  将覆盖：$PLUGIN_DIR/fish_core/data/"; fi
if [ "$DO_IT" != 1 ]; then
  echo "  这是演练。确认无误后加 --yes 执行。"
  exit 0
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
QDIR="$DATA_DIR/quarantine"
mkdir -p "$QDIR"
chmod 700 "$QDIR"

echo "== 4. 停服 =="
systemctl --user stop "$SERVICE" || echo "  [WARN] 停服失败，继续（文件可能被占用）"

echo "== 5. 备份现库 =="
if [ -f "$DATA_DIR/fish_memory.db" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DATA_DIR/fish_memory.db" ".backup '$QDIR/pre-restore-$STAMP.db'"
  else
    cp "$DATA_DIR/fish_memory.db" "$QDIR/pre-restore-$STAMP.db"
  fi
  chmod 600 "$QDIR/pre-restore-$STAMP.db" 2>/dev/null || true
  echo "  已存：$QDIR/pre-restore-$STAMP.db"
fi

echo "== 6. 换入 =="
# 旧 wal/shm 是旧库的，必须一起清掉，否则新库 + 旧日志 = 校验失败
rm -f "$DATA_DIR/fish_memory.db-wal" "$DATA_DIR/fish_memory.db-shm"
install -m 600 "$WORK/data/fish_memory.db" "$DATA_DIR/fish_memory.db"
for f in "$WORK"/data/*.json; do
  [ -e "$f" ] || continue
  cp -p "$f" "$DATA_DIR/"
done
if [ -f "$WORK/config/astrbot_plugin_fish_config.json" ]; then
  cp -p "$WORK/config/astrbot_plugin_fish_config.json" "$CONFIG_DIR/"
fi
if [ "$TEMPLATES" = 1 ] && [ -d "$WORK/plugin/fish_core/data" ]; then
  rm -rf "$PLUGIN_DIR/fish_core/data"
  cp -a "$WORK/plugin/fish_core/data" "$PLUGIN_DIR/fish_core/data"
fi

if [ "$RESTART" = 1 ]; then
  echo "== 7. 起服 =="
  systemctl --user start "$SERVICE"
  sleep 3
  systemctl --user is-active "$SERVICE" || { echo "服务没起来，检查 journalctl --user -u $SERVICE"; exit 1; }
fi

echo "== 完成 =="
echo "  回滚：把 $QDIR/pre-restore-$STAMP.db 放回 $DATA_DIR/fish_memory.db（先停服、删 wal/shm）"
