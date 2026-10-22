@echo off
REM ============================================================
REM wb-migrate v3.2.0 — WorkBuddy 全局备份与迁移工具 (Windows)
REM 用法:
REM   wb-migrate.bat [全局参数] scan|modules|backup|info|verify|restore|migrate|rollback|compat|selftest|accounts|switch [参数...]
REM 全局参数 (--wb-home / -v / -q) 必须放在子命令之前
REM
REM 同设备多账号切换:
REM   wb-migrate.bat accounts                          # 列出本机账号 (只读)
REM   wb-migrate.bat switch --to <USER_ID> --dry-run   # 预览切换 (零写入)
REM   wb-migrate.bat switch --to <USER_ID>             # 正式切换 (自动备份 + 失败回滚)
REM   wb-migrate.bat switch --to <USER_ID> --config-policy merge --db-mode stash
REM
REM Windows 说明:
REM   - 需 Python 3.9+ (python.org 官方安装包或 Microsoft Store 版均可), 无需任何第三方库
REM   - 自动探测 python / python3 / py -3 启动器; 自动切换到 UTF-8 代码页避免中文乱码
REM   - 建议"以普通用户"运行 (不要用管理员), 恢复前请完全退出 WorkBuddy (含托盘图标)
REM   - 账号切换检测到 WorkBuddy 仍在运行会直接阻断 (退出码 3): 账号数据被占用无法移动
REM ============================================================
setlocal enabledelayedexpansion

REM --- 编码: 切到 UTF-8 代码页并强制 Python 使用 UTF-8 IO, 避免中文路径/输出乱码 ---
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONWARNDEFAULTENCODING=

set SCRIPT_DIR=%~dp0
set PYTHON_SCRIPT=%SCRIPT_DIR%scripts\wb_migrate.py

if not exist "%PYTHON_SCRIPT%" (
    echo [错误] 未找到脚本: %PYTHON_SCRIPT%
    exit /b 1
)

REM --- 探测 Python 解释器: python -> python3 -> py -3 启动器 ---
set PYTHON_CMD=
where python >nul 2>&1
if %ERRORLEVEL% EQU 0 goto :found_python
where python3 >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set PYTHON_CMD=python3
    goto :found_launcher
)
where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set PYTHON_CMD=py -3
    goto :found_launcher
)
echo [错误] 未找到 Python 运行环境。
echo        请安装 Python 3.9 或更高版本 (https://www.python.org/downloads/windows/)
echo        安装时勾选 "Add python.exe to PATH", 然后重新打开命令行。
exit /b 3

:found_python
set PYTHON_CMD=python

:found_launcher
"%PYTHON_CMD%" "%PYTHON_SCRIPT%" %*
set RC=%ERRORLEVEL%

REM --- 退出码透传: 0 成功 / 1 错误 / 2 校验失败 / 3 权限不足 / 4 中断 ---
if "%RC%"=="2" echo [结果] 备份包校验失败 (未经完整校验的包不会被恢复)
if "%RC%"=="3" echo [结果] 权限不足: 请检查是否被安全软件拦截, 不要在系统目录写入
if "%RC%"=="4" echo [结果] 已安全中断: 备份可用 --resume 续传, 恢复可用 rollback --latest 撤销
endlocal & exit /b %RC%
