#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb-migrate v3.3.0 / wb_auto.py — 免手动点击的自动恢复编排器 (Windows)

痛点: Windows 下 WorkBuddy 运行时 workbuddy.db 被进程锁住, 恢复会被
EXIT_PERMISSION(3) 直接拒绝。传统做法是「手动退出 → 手动双击 bat」,
容易漏掉托盘图标。

本脚本把这一步也自动化:
  1. 注册一个计划任务作为看门狗 (Task Scheduler 拥有进程, 不随会话退出)
  2. 看门狗轮询 WorkBuddy 进程, 一旦消失 → 等句柄释放 → 自动恢复
  3. 恢复后合并专家插件注册表 (plugins/installed_plugins.json)
  4. 可选自动重新拉起 WorkBuddy
  5. 全程写日志, 结束后自删计划任务

用法:
  # A) 注册看门狗(推荐): 立刻返回, 之后你随手退出 WorkBuddy 即可
  python wb_auto.py restore <pkg.tar.gz> --relaunch

  # B) 已经退出 WorkBuddy, 立即执行
  python wb_auto.py restore <pkg.tar.gz> --now

  # 查看看门狗状态 / 取消
  python wb_auto.py status
  python wb_auto.py cancel

内部入口(计划任务调用, 勿手动执行):
  python wb_auto.py __watch <params.json>
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TASK_NAME = "WBMigrateAutoRestore"
AUTO_DIRNAME = ".wb-migrate-auto"
EXIT_OK, EXIT_ERROR, EXIT_CHECKSUM, EXIT_PERM, EXIT_INT = 0, 1, 2, 3, 4
DEFAULT_MODULES = "conversations,memory,tasks,automations,experts"

_LOG_FILE: Path | None = None


# ---------------------------------------------------------------- 基础设施
def _log(msg: str) -> None:
    line = "[%s] %s" % (_dt.datetime.now().strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def wb_home() -> Path:
    return Path(os.environ.get("WB_MIGRATE_HOME") or (Path.home() / ".workbuddy"))


def skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def migrate_py() -> Path:
    return Path(__file__).resolve().parent / "wb_migrate.py"


def py_exe() -> str:
    return sys.executable


# ---------------------------------------------------------------- 进程检测
def wb_running() -> bool:
    """WorkBuddy 是否在运行 (按镜像名, 不区分大小写)。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        ).stdout
    except Exception:
        return False
    for row in out.splitlines():
        cell = row.split('","')[0].strip('"').strip().strip('"')
        if cell.lower() in ("workbuddy.exe", "workbuddy"):
            return True
    return False


def wait_for_exit(timeout_sec: int, poll: int = 3) -> bool:
    _log("等待 WorkBuddy 退出 (最长 %d 秒)... 请完全退出含托盘图标" % timeout_sec)
    t0 = time.time()
    announced = False
    while time.time() - t0 < timeout_sec:
        if not wb_running():
            _log("检测到 WorkBuddy 已退出 (等待 %.0f 秒)" % (time.time() - t0))
            return True
        if not announced:
            _log("WorkBuddy 仍在运行, 每 %d 秒轮询一次..." % poll)
            announced = True
        time.sleep(poll)
    _log("等待超时 (%d 秒), WorkBuddy 仍未退出 —— 放弃本次自动恢复" % timeout_sec)
    return False


# ---------------------------------------------------------------- 恢复主流程
def run_restore(pkg: str, modules: str, target_uid: str | None,
                conflict: str, extra: list[str]) -> int:
    cmd = [py_exe(), str(migrate_py()), "restore", pkg,
           "--modules", modules, "--conflict", conflict, "--auto-map"]
    if target_uid:
        cmd += ["--target-user-id", target_uid]
    cmd += extra
    _log("执行: " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, text=True, encoding="utf-8",
                              errors="replace", capture_output=True)
    except Exception as exc:
        _log("恢复进程启动失败: %r" % exc)
        return EXIT_ERROR
    for line in (proc.stdout or "").splitlines():
        _log("  " + line)
    for line in (proc.stderr or "").splitlines():
        _log("  [stderr] " + line)
    _log("恢复退出码: %d" % proc.returncode)
    return proc.returncode


def merge_plugin_registry(pkg: str, policy: str = "merge") -> tuple[int, int, str]:
    """把备份包内 plugins/installed_plugins.json 的 plugins 记录并入本机。

    专家实体文件恢复后, 若注册表没有对应条目, App 仍不会显示该专家。
    返回 (新增, 跳过, 说明)。
    """
    target = wb_home() / "plugins" / "installed_plugins.json"
    try:
        with tarfile.open(pkg, "r:gz") as tf:
            member = None
            for m in tf.getmembers():
                n = m.name.replace("\\", "/")
                if n.endswith("plugins/installed_plugins.json") and m.isfile():
                    member = m
            if member is None:
                return 0, 0, "包内无 installed_plugins.json, 跳过注册表合并"
            raw = tf.extractfile(member).read()
        src = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return 0, 0, "读取包内注册表失败: %r" % exc

    src_plugins = src.get("plugins") or {}
    if not isinstance(src_plugins, dict):
        return 0, 0, "注册表结构异常, 跳过"

    tgt: dict = {}
    if target.is_file():
        try:
            tgt = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            tgt = {}
        backup = target.with_suffix(".json.wb-bak-%s" % _dt.datetime.now().strftime("%Y%m%d%H%M%S"))
        try:
            backup.write_bytes(target.read_bytes())
        except Exception:
            pass

    tgt_plugins = tgt.setdefault("plugins", {})
    if "version" not in tgt:
        tgt["version"] = src.get("version", 1)

    added = skipped = 0
    for key, val in src_plugins.items():
        if key not in tgt_plugins:
            tgt_plugins[key] = val
            added += 1
        elif policy == "overwrite":
            tgt_plugins[key] = val
            added += 1
        else:
            skipped += 1

    if added == 0:
        return 0, skipped, "注册表无新增条目 (已跳过 %d)" % skipped

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.part")
        tmp.write_text(json.dumps(tgt, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except Exception as exc:
        return 0, skipped, "写回注册表失败: %r" % exc
    return added, skipped, "注册表合并完成: 新增 %d, 跳过 %d" % (added, skipped)


def remap_custom_experts(target_uid: str | None) -> str:
    """自定义专家按账号存放: experts/custom/<uid>/。

    账号合并只改 sessions/automations 的 user_id, 不会动这个目录, 于是旧账号的
    自定义专家在切换后「看不见」。这里把其他 uid 目录的内容合并进目标 uid 目录
    (只补缺失, 不覆盖、不删除源)。
    """
    if not target_uid:
        return "未指定目标账号, 跳过自定义专家重映射"
    base = wb_home() / "experts" / "custom"
    if not base.is_dir():
        return "无 experts/custom 目录, 跳过"

    dst = base / target_uid
    copied = 0
    for src in list(base.iterdir()):
        if not src.is_dir() or src.name == target_uid:
            continue
        for item in src.rglob("*"):
            rel = item.relative_to(src)
            tgt = dst / rel
            try:
                if item.is_dir():
                    tgt.mkdir(parents=True, exist_ok=True)
                elif not tgt.exists():
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, tgt)
                    copied += 1
            except Exception as exc:
                _log("  自定义专家复制失败 %s: %r" % (rel, exc))
    return "自定义专家合并: 补充 %d 个文件 -> %s" % (copied, target_uid[:8] + "...")


def relaunch_wb() -> None:
    """重新拉起 WorkBuddy (尽力而为, 找不到就提示)。"""
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "WorkBuddy" / "WorkBuddy.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "WorkBuddy" / "WorkBuddy.exe",
    ]
    for exe in candidates:
        try:
            if exe.is_file():
                subprocess.Popen([str(exe)], close_fds=True)
                _log("已重新启动 WorkBuddy: %s" % exe)
                return
        except Exception:
            continue
    _log("未找到 WorkBuddy.exe, 请手动重新打开")


def pipeline(params: dict) -> int:
    global _LOG_FILE
    log_path = Path(params["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = log_path

    _log("=" * 62)
    _log("wb-migrate 自动恢复 (v3.3.0)")
    _log("包: %s" % params["pkg"])
    _log("模块: %s" % params["modules"])
    _log("=" * 62)

    if not wait_for_exit(int(params.get("timeout", 3600))):
        return EXIT_INT

    settle = int(params.get("settle", 8))
    _log("等待 %d 秒让文件句柄释放..." % settle)
    time.sleep(settle)

    if wb_running():
        _log("WorkBuddy 又被启动, 中止以避免数据库锁冲突")
        return EXIT_PERM

    rc = run_restore(params["pkg"], params["modules"],
                     params.get("target_uid"), params.get("conflict", "merge"),
                     params.get("extra") or [])

    if rc == EXIT_OK and not params.get("no_registry"):
        added, skipped, note = merge_plugin_registry(
            params["pkg"], params.get("registry_policy", "merge"))
        _log("专家注册表: %s" % note)
        _log("自定义专家: %s" % remap_custom_experts(params.get("target_uid")))
    elif rc != EXIT_OK:
        _log("恢复未成功 (rc=%d), 跳过注册表合并" % rc)

    _log("-" * 62)
    if rc == EXIT_OK:
        _log("结果: 成功")
    elif rc == EXIT_PERM:
        _log("结果: 被占用, 未执行")
    elif rc == EXIT_CHECKSUM:
        _log("结果: 校验失败")
    else:
        _log("结果: 失败 rc=%d" % rc)
    _log("回滚命令: wb-migrate rollback --latest")
    _log("-" * 62)

    if params.get("relaunch"):
        relaunch_wb()

    cleanup_task()
    return rc


# ---------------------------------------------------------------- 计划任务
def _schtasks(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["schtasks", *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=60)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return 1, repr(exc)


def cleanup_task() -> None:
    rc, out = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if rc == 0:
        _log("已清理计划任务 %s" % TASK_NAME)


def schedule_watchdog(params: dict) -> int:
    auto_dir = wb_home() / AUTO_DIRNAME
    auto_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    params_path = auto_dir / ("params-%s.json" % ts)
    params_path.write_text(json.dumps(params, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    # 用 .cmd 包装, 避免 schtasks /TR 的引号嵌套地狱
    cmd_path = auto_dir / ("run-%s.cmd" % ts)
    cmd_path.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "set PYTHONIOENCODING=utf-8\r\n"
        "\"%s\" \"%s\" __watch \"%s\"\r\n"
        % (py_exe(), Path(__file__).resolve(), params_path),
        encoding="utf-8",
    )

    rc, out = _schtasks("/Create", "/TN", TASK_NAME, "/TR", str(cmd_path),
                        "/SC", "ONCE", "/ST", "23:59", "/RL", "LIMITED", "/F")
    if rc != 0:
        _log("创建计划任务失败: %s" % out.strip())
        _log("可退回手动方式: python %s restore \"%s\" --now"
             % (Path(__file__).resolve(), params["pkg"]))
        return EXIT_ERROR

    rc, out = _schtasks("/Run", "/TN", TASK_NAME)
    if rc != 0:
        _log("启动计划任务失败: %s" % out.strip())
        return EXIT_ERROR

    _log("看门狗已就绪 (计划任务: %s)" % TASK_NAME)
    _log("现在可以完全退出 WorkBuddy —— 退出后会自动恢复%s"
         % (", 并自动重新打开" if params.get("relaunch") else ""))
    _log("日志: %s" % params["log"])
    _log("取消: python %s cancel" % Path(__file__).resolve())
    return EXIT_OK


# ---------------------------------------------------------------- CLI
def _build_default_log() -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return str(wb_home() / "logs" / ("wb-auto-%s.log" % ts))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wb_auto", description="wb-migrate 自动恢复编排器")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("restore", help="恢复(可等待 WorkBuddy 退出后自动执行)")
    p.add_argument("archive")
    p.add_argument("--modules", default=DEFAULT_MODULES)
    p.add_argument("--target-user-id", dest="target_uid")
    p.add_argument("--conflict", default="merge", choices=["merge", "overwrite", "rename"])
    p.add_argument("--relaunch", action="store_true", help="恢复后自动重新打开 WorkBuddy")
    p.add_argument("--now", action="store_true", help="立即执行(需已退出 WorkBuddy)")
    p.add_argument("--timeout", type=int, default=3600, help="等待退出的最长秒数")
    p.add_argument("--settle", type=int, default=8, help="进程消失后额外等待秒数")
    p.add_argument("--log")
    p.add_argument("--no-registry", action="store_true", help="不合并专家插件注册表")
    p.add_argument("--registry-policy", default="merge", choices=["merge", "overwrite"])
    p.add_argument("--extra", nargs=argparse.REMAINDER, help="透传给 wb_migrate restore 的其余参数")

    sub.add_parser("status", help="看看门狗是否在跑")
    sub.add_parser("cancel", help="取消看门狗")

    w = sub.add_parser("__watch")
    w.add_argument("params")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return EXIT_ERROR

    if args.cmd == "__watch":
        return pipeline(json.loads(Path(args.params).read_text(encoding="utf-8")))

    if args.cmd == "status":
        rc, out = _schtasks("/Query", "/TN", TASK_NAME)
        print(out.strip() or "(无输出)")
        print("WorkBuddy 运行中: %s" % wb_running())
        return EXIT_OK if rc == 0 else EXIT_ERROR

    if args.cmd == "cancel":
        cleanup_task()
        return EXIT_OK

    # restore
    pkg = str(Path(args.archive).expanduser().resolve())
    if not Path(pkg).is_file():
        print("备份包不存在: %s" % pkg, file=sys.stderr)
        return EXIT_ERROR

    params = {
        "pkg": pkg,
        "modules": args.modules,
        "target_uid": args.target_uid,
        "conflict": args.conflict,
        "relaunch": bool(args.relaunch),
        "timeout": args.timeout,
        "settle": args.settle,
        "log": args.log or _build_default_log(),
        "no_registry": bool(args.no_registry),
        "registry_policy": args.registry_policy,
        "extra": [x for x in (args.extra or []) if x != "--"],
    }

    if args.now:
        return pipeline(params)

    if not wb_running():
        _log("WorkBuddy 未运行, 直接执行恢复")
        return pipeline(params)

    return schedule_watchdog(params)


if __name__ == "__main__":
    sys.exit(main())
