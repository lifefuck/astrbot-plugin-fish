#!/usr/bin/env bash
# 把隐私扫描挂到 pre-commit。装一次就够，提交时自动跑。
set -e
cd "$(dirname "$0")/.."
cat > .git/hooks/pre-commit <<'HOOK'
#!/usr/bin/env bash
# 只扫暂存区，被拦就别提交
exec python3 scripts/privacy_scan.py
HOOK
chmod +x .git/hooks/pre-commit
echo "pre-commit 已安装：$(pwd)/.git/hooks/pre-commit"
