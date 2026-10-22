#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb-migrate 跨平台启动器

SkillHub 分发的技能包不允许携带 .bat / .sh，所以把这两个入口承担的三件事
合并到这里，macOS / Windows / Linux 用同一条命令：

  1. 解释器检查  —— 要求 Python 3.9+；当前解释器过低时自动探测 py -3 / python3 重启自己
  2. 编码兜底    —— Windows 控制台默认 GBK，强制 stdout/stderr 走 UTF-8 + errors=replace
  3. 参数与退出码透传 —— 原样转给同目录的 wb_migrate.py

用法:
  python3 scripts/wb.py scan
  python3 scripts/wb.py backup -o ~/Desktop
  python3 scripts/wb.py restore ~/Desktop/wb-backup-xxx.tar.gz --auto-map
  python scripts\\wb.py compat          (Windows)

本文件只是入口封装，不含任何业务逻辑；真正的引擎是同目录的 wb_migrate.py。
"""
from __future__ import annotations

import os
import sys

MIN_VERSION = (3, 9)
MIN_TAG = MIN_VERSION[0] * 100 + MIN_VERSION[1]      # 309

# 版本过低时按顺序尝试的解释器（py 需要额外补 -3）
CANDIDATES = (
    ("python3", []),
    ("python", []),
    ("py", ["-3"]),
)


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _probe(cmd):
    """返回候选解释器的主次版本号整数（如 313），不可用返回 None。"""
    import subprocess

    try:
        proc = subprocess.run(
            list(cmd) + ["-c", "import sys;print(sys.version_info[0]*100+sys.version_info[1])"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    return int(out) if out.isdigit() else None


def _relaunch_with_newer() -> bool:
    """当前解释器太旧时，找一个 >= 3.9 的解释器接管。成功则不返回。"""
    import shutil

    seen = []
    for name, extra in CANDIDATES:
        exe = shutil.which(name)
        if not exe:
            continue
        tag = _probe([exe] + extra)
        seen.append("%s=%s" % (name, tag if tag else "?"))
        if tag is not None and tag >= MIN_TAG:
            sys.stderr.write("当前解释器过旧，切换到 %s 重新执行……\n" % exe)
            sys.stderr.flush()
            os.execv(exe, [exe] + extra + [os.path.abspath(__file__)] + sys.argv[1:])
            return True          # 理论上到不了这里
    sys.stderr.write("没有找到可用的 Python %d.%d+（探测结果: %s）\n"
                     % (MIN_VERSION[0], MIN_VERSION[1], ", ".join(seen) or "无"))
    return False


def main() -> int:
    _force_utf8()

    if sys.version_info < MIN_VERSION:
        sys.stderr.write(
            "wb-migrate 需要 Python %d.%d 或更高版本，当前是 %d.%d。\n"
            % (MIN_VERSION[0], MIN_VERSION[1], sys.version_info[0], sys.version_info[1]))
        _relaunch_with_newer()
        sys.stderr.write("请安装 Python 3.9+ 后重试：https://www.python.org/downloads/\n")
        return 1

    here = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(here, "wb_migrate.py")
    if not os.path.isfile(target):
        sys.stderr.write("找不到引擎脚本：%s\n"
                         "请确认整个 wb-migrate 目录被完整复制（scripts/ 下应有 "
                         "wb_migrate.py、wb_auto.py、wb.py）。\n" % target)
        return 1

    import runpy

    sys.argv = [target] + sys.argv[1:]
    try:
        runpy.run_path(target, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        sys.stderr.write("%s\n" % code)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
