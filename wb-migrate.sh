#!/usr/bin/env bash
# wb-migrate — WorkBuddy 全局备份与迁移工具 v3.2.0 (macOS/Linux)
# 用法: ./wb-migrate.sh [全局参数] <scan|modules|backup|info|verify|restore|migrate|rollback|compat|selftest|accounts|switch> [参数...]
# 注意: 全局参数 (--wb-home/-v/-q) 须放在子命令之前
#
# 同设备多账号切换:
#   ./wb-migrate.sh accounts                          # 列出本机账号 (只读)
#   ./wb-migrate.sh switch --to <USER_ID> --dry-run   # 预览切换 (零写入)
#   ./wb-migrate.sh switch --to <USER_ID>             # 正式切换 (自动备份 + 失败回滚)
#   ./wb-migrate.sh switch --to <USER_ID> --config-policy merge --db-mode stash
#
# 支持: WorkBuddy 5.6-5.9 / macOS 12+ / Linux 4.15+ / Python 3.9+ (仅标准库)
# 退出码: 0 成功 / 1 错误 / 2 校验失败 / 3 权限不足 / 4 中断
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/scripts/wb_migrate.py"
export PYTHONIOENCODING=utf-8
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo "错误: 未找到 Python 3, 请先安装 Python 3.9+"; exit 1
fi
"$PYTHON" "$PYTHON_SCRIPT" "$@"
RC=$?
if [ "$RC" = "2" ]; then echo "[结果] 备份包校验失败: 该包未被恢复, 目标数据未改动"; fi
if [ "$RC" = "3" ]; then echo "[结果] 权限不足: 检查目录写权限或安全沙箱授权"; fi
if [ "$RC" = "4" ]; then echo "[结果] 已安全中断: 备份 --resume <ID> 续传 / 恢复 rollback --latest 撤销"; fi
exit $RC
