@echo off
REM wb-migrate — WorkBuddy 数据迁移工具 (Windows)
REM 用法: wb-migrate.bat scan|backup|restore|info|migrate [参数...]

setlocal
set SCRIPT_DIR=%~dp0
set PYTHON_SCRIPT=%SCRIPT_DIR%scripts\wb_migrate.py

REM 查找 Python
where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set PYTHON=python
) else (
    where python3 >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set PYTHON=python3
    ) else (
        echo 错误: 未找到 Python，请先安装 Python 3.9+ 并添加到 PATH
        exit /b 1
    )
)

"%PYTHON%" "%PYTHON_SCRIPT%" %*
