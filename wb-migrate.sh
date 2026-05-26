#!/usr/bin/env bash
# wb-migrate — WorkBuddy 数据迁移工具 (macOS / Linux)
# 用法: ./wb-migrate.sh scan|backup|restore|info|migrate [参数...]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/scripts/wb_migrate.py"

# 查找可用的 Python 3
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo "错误: 未找到 Python 3，请先安装 Python 3.9+"
    exit 1
fi

exec "$PYTHON" "$PYTHON_SCRIPT" "$@"
