#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-migrate — WorkBuddy 全局备份与迁移工具 v3.0.0

适配 WorkBuddy (国内版) 5.8.x 目录结构与数据库 schema，同时保留对 v1/v2 旧备份包的读取兼容。

核心能力:
  - 12 大模块全量 / 按模块选择性导出 (对话/任务/记忆/配置/连接器/专家/定时任务/技能/产物/分享文件/应用/助理)
  - 带版本号与 sha256 校验清单的结构化备份包 (manifest.json + checksums.sha256)
  - 三种冲突处理策略: merge(默认) / overwrite / rename
  - 恢复前自动快照 + rollback 命令回滚
  - 敏感信息默认脱敏 (token/key/凭据), --no-redact 关闭
  - 跨平台 (macOS/Windows/Linux) 路径映射, 含同系统换用户名场景
  - 进度与日志双输出, 原子写入, 中断后可 --resume 重试或 rollback 安全中止

用法:
    python3 wb_migrate.py scan
    python3 wb_migrate.py modules
    python3 wb_migrate.py backup  [-o DIR] [--modules a,b] [--no-redact] ...
    python3 wb_migrate.py info    <archive.tar.gz> [--verify]
    python3 wb_migrate.py verify  <archive.tar.gz>
    python3 wb_migrate.py restore <archive.tar.gz> [--conflict merge|overwrite|rename]
                                  [--modules a,b] [--auto-map] [--dry-run] ...
    python3 wb_migrate.py rollback [--latest|--id ID] [--dry-run] [--prune --keep N]
    python3 wb_migrate.py accounts   # 本机账号清单 (多账号切换场景)
    python3 wb_migrate.py switch --to <USER_ID> [--config-policy keep|overwrite|merge]
                                     [--db-mode keep|merge|stash] [--dry-run]
    python3 wb_migrate.py compat     # 环境兼容性自检报告
    python3 wb_migrate.py selftest   # 跨平台一致性用例 (新机器/Windows 上验证)

兼容性: Python 3.9+ (仅标准库); WorkBuddy 5.6 - 5.9+; macOS/Windows/Linux。
"""

import argparse
import atexit
import fnmatch
import subprocess
import tempfile
import time
import zlib
import gzip
import hashlib
import io
import json
import logging
import os
import platform
import random
import re
import shutil
import signal
import sqlite3
import sys
import tarfile
import zlib
import datetime
from pathlib import Path, PureWindowsPath
from typing import Optional, Dict, List, Tuple, Set, Iterable

# ============================================================
# [S1] 常量与版本
# ============================================================

VERSION = "3.3.0"
BACKUP_FORMAT_VERSION = 3
SUPPORTED_FORMAT_VERSIONS = {1, 2, 3}

# ------------------------------------------------------------
# 环境兼容性矩阵 (见 README "环境兼容性" 章节)
# ------------------------------------------------------------
WB_TARGET_VERSION = "5.9.0"      # 已实测的目标版本
WB_MIN_VERSION = "5.6.0"         # 低于此版本仅允许读取式备份, 恢复需降级确认
WB_TESTED_VERSIONS = ["5.6.x", "5.7.x", "5.8.x", "5.9.0"]
PY_MIN_VERSION = (3, 9)          # 兼容 Python 3.9+ (不使用 match / X|Y 等新语法)
WINDOWS_MIN_BUILD = 17763        # Windows 10 1809 (长路径注册表键起作用的基线)
WINDOWS_MIN_NAME = "Windows 10 1809"
MACOS_MIN_VERSION = (12, 0)      # macOS 12 Monterey
LINUX_MIN_KERNEL = (4, 15)

# WorkBuddy 数据格式演进过程中出现过的目录/文件; 用于版本推理兜底
WB_VERSION_HINT_FILES = ("startup-seed-version.json", "install-timing-reported")
UUID_DIR_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
REDACTION_PLACEHOLDER = "***WB-REDACTED***"
REDACTION_RULES_VERSION = 1
HISTORY_DIRNAME = ".wb-migrate-history"
ACCOUNTS_DIRNAME = ".wb-migrate-accounts"     # 同设备多账号隔离区
ACCOUNT_META_FILE = "account.json"
ACCOUNT_DATA_DIRNAME = "data"                 # 账号专属数据 (按 wb_home 相对路径存放)
ACCOUNT_SHARED_DIRNAME = "shared"             # 全局共享配置副本 (用于冲突处理)
ACCOUNT_ROWS_FILE = "db-rows.json"            # 账号维度数据库行副本
SWITCH_REPORT_FILE = "switch-report.json"
STAGING_DIRNAME = ".wb-migrate-tmp"
PERMISSION_TEST_FILENAME = ".wb_migrate_permission_test"
LOG_DIRNAME = "logs"

DB_MAIN = "workbuddy.db"
DB_EDGE = "edge-sync-mapping-v4.db"

# 退出码
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHECKSUM = 2
EXIT_PERMISSION = 3
EXIT_INTERRUPTED = 4


class ChecksumError(Exception):
    """备份包校验失败"""


class InterruptedError_(Exception):
    """用户中断"""


# (table, pk, module)
DB_TABLES: List[Tuple[str, str, str]] = [
    ("sessions", "id", "conversations"),
    ("session_usage", "session_id", "conversations"),
    ("workspaces", "path", "conversations"),
    ("workspace_trust_decisions", "workspace_key", "conversations"),
    ("automations", "id", "automations"),
    ("automation_runs", "thread_id", "automations"),
    ("automation_runtime_state", "automation_id", "automations"),
    ("automation_delivery_outbox", "id", "automations"),
    ("buddy_snapshots", "snapshot_id", "apps"),
    ("migration_meta", "key", "config"),
]

DB_TABLES_SKIP = {"__workbuddy_drizzle_migrations"}

# 模块定义: dirs/files 支持通配 (相对 wb_home)
MODULES: List[Dict] = [
    {"id": "conversations", "label": "对话",
     "dirs": ["projects", "workspace", "sessions"], "files": [],
     "tables": ["sessions", "session_usage", "workspaces", "workspace_trust_decisions"]},
    {"id": "tasks", "label": "任务",
     "dirs": ["tasks", "plans", "todos"], "files": [], "tables": []},
    {"id": "memory", "label": "记忆",
     "dirs": ["memory", "memery", "storage/user-*"], "files": [], "tables": []},
    {"id": "config", "label": "配置",
     "dirs": ["local_storage", "cli-env", "project-resources"],
     "files": ["settings.json", "argv.json", "models.json", "user-state.json",
               "workspace-state.json", "failover.json", ".policy-cache.json",
               "mcp-approvals.json"],
     "tables": ["migration_meta"]},
    {"id": "connectors", "label": "连接器",
     "dirs": ["connectors"], "files": ["mcp.json"], "tables": []},
    # [v3.3.0] 5.9 起专家实体全部落在 plugins/cache/experts/<slug>/<version>/，
    # plugins/ 又在 ALWAYS_EXCLUDE_TOP_DIRS 里，只写 "experts" 会漏掉 100% 的专家。
    {"id": "experts", "label": "专家",
     "dirs": ["experts",
              "plugins/cache/experts",
              "plugins/marketplaces/experts",
              "plugins/marketplaces/my-experts"],
     "files": ["expert-history.json",
               "plugins/installed_plugins.json",
               "plugins/configured-plugins.json",
               "plugins/known_marketplaces.json"],
     "tables": []},
    {"id": "automations", "label": "定时任务",
     "dirs": ["automations", "automation-backups"], "files": [],
     "tables": ["automations", "automation_runs", "automation_runtime_state",
                "automation_delivery_outbox"]},
    {"id": "skills", "label": "技能",
     "dirs": ["skills"], "files": [], "tables": []},
    {"id": "artifacts", "label": "产物",
     "dirs": ["blobs", "clipboard-images", "artifact-index", "media-index",
              "file-history", "changes-index", "changes-detail",
              "file-tree-manifests", "shell-snapshots"],
     "large_dirs": ["traces"], "files": [], "tables": []},
    {"id": "shared", "label": "分享文件",
     "dirs": ["cloudstudio-deploy-history"], "files": [], "tables": []},
    {"id": "apps", "label": "应用",
     "dirs": [], "large_dirs": ["app", "appearance-resources"], "files": [],
     "tables": ["buddy_snapshots"]},
    {"id": "assistant", "label": "助理",
     "dirs": ["user-*", "inspiration", "@uuid-dirs"],
     "files": ["BOOTSTRAP.md", "IDENTITY.md", "MEMORY.md", "USER.md", "SOUL.md"],
     "tables": []},
]

# 动态目录标记: 在 iter_module_dirs 中按规则展开 (不依赖固定命名)
#   @uuid-dirs : ~/.workbuddy/<uuid>/ 旧版本遗留的账号根目录 (5.x 早期写法)
DYNAMIC_DIR_TOKENS = {"@uuid-dirs"}

MODULE_IDS = [m["id"] for m in MODULES]

# 始终排除 (相对 wb_home 的 glob, 作用于完整相对路径或单段名)
# 5.6-5.9 通用; 其中带 (*) 的是跨机语义有害项, 恢复后会让目标端误判迁移/缓存状态
ALWAYS_EXCLUDE_PATTERNS = [
    "*-wal", "*-shm", ".DS_Store", "__pycache__", "*.pyc",
    "skills/_bm_skillid_migration.json",
    "memory/.backup-*", "memery/.backup-*",
    "security/threat-database*",
    # --- 5.6~5.9 机器态/运行时缓存 ---
    "desktop_conversation_migrated",          # (*) 桌面端会话迁移标记
    ".migration-history",                     # (*) 迁移执行历史
    ".migration-history/*",
    ".workbuddy-sqlite-migrations",           # schema 迁移 SQL, 随 App 版本
    ".workbuddy-sqlite-migrations/*",
    "workbuddy.backup-*",                     # App 产生的历史备份
    "workbuddy.db.backup-*",
    "epoch-marker-*.json",                    # (*) 运行期标记
    "install-timing-reported",
    "last-launch.json",
    "tencent-docs-engine.port",               # 本地端口
    "usage-log.json",                         # 遥测/用量缓存
    "qimei-cache.json",
    "plugin-status-daily.json",
    "mcp-tool-list.json",                     # MCP 工具列表缓存, 启动自动重建
    "extension-registry",                     # 扩展注册表缓存
    "external-packages",
    # --- [v3.3.0] 专家插件缓存里的机器态 ---
    ".in_use",                                # (*) pid 锁文件, 跨机无意义
    "plugins/cache/experts/*/*/.in_use",
    ".downloaded_at",                         # (*) 下载时间戳
]

# 始终排除的顶层目录 (不属于任何模块且永不备份)
ALWAYS_EXCLUDE_TOP_DIRS = {
    "binaries", "plugins", "logs", "cache", "chrome-devtools-profile",
    "skills-marketplace", "connectors-marketplace", "extensions",
    "plugin-marketplace-state", "plugin-marketplace-state-new",
    "bin", "wbipc", "editor-sdk-sandbox-ws", "security", "audit-log",
    ACCOUNTS_DIRNAME,   # 多账号隔离区: 不属于任何模块, 也不参与备份/解包
    "workspace",  # 归属 conversations 模块单独处理, 不作为 other 出现
    HISTORY_DIRNAME, STAGING_DIRNAME,
}

# 脱敏: 整文件省略 (glob, 相对 wb_home)
REDACT_OMIT_PATTERNS = [
    "keyblob", "device-id", "login-device-nonce",
    "connectors/*/.master.key",
    "connectors/*/.master.key.*",
]

# 脱敏: JSON 键名子串匹配 (botToken / clientSecret / accessToken 等复合键)
SENSITIVE_KEY_SUBSTRINGS = (
    "token", "secret", "password", "passwd", "apikey", "api_key", "access_key",
    "accesskey", "authorization", "bearer", "cookie", "envelope", "credential",
    "clientsecret", "privatekey", "keyblob", "salt", "nonce",
)


def _key_is_sensitive(key: str) -> bool:
    k = str(key).lower().replace("-", "").replace("_", "")
    return any(s.replace("_", "") in k for s in SENSITIVE_KEY_SUBSTRINGS)


def _redact_json_obj(obj, key: Optional[str] = None):
    """递归: 键名命中敏感规则且值为标量 -> 占位符; 容器值继续向下递归"""
    if key is not None and _key_is_sensitive(key) \
            and isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
        return REDACTION_PLACEHOLDER
    if isinstance(obj, dict):
        return {k: _redact_json_obj(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_json_obj(v) for v in obj]
    return obj

# mcp.json URL query 中需要脱敏的参数名
URL_QUERY_SENSITIVE = {"apikey", "api_key", "token", "key", "access_token", "secret"}

_LOG = logging.getLogger("wb-migrate")
_INTERRUPTED = {"flag": False}
_CLEANUP_PATHS: List[str] = []


# ============================================================
# [S1] 平台 / 路径 / 日志 / 进度 / 信号
# ============================================================

def get_platform_info() -> Dict[str, str]:
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "username": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "hostname": platform.node().split(".")[0] or "unknown",
        "home": str(Path.home()),
        "path_sep": os.sep,
    }


def is_windows() -> bool:
    return platform.system() == "Windows"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_linux() -> bool:
    return platform.system() == "Linux"


# ------------------------------------------------------------
# Windows 文件系统适配层
# ------------------------------------------------------------

# 保留设备名 (任何扩展名均非法)
WIN_RESERVED_NAMES = set(["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"] +
                         ["COM%d" % i for i in range(1, 10)] +
                         ["LPT%d" % i for i in range(1, 10)])
WIN_ILLEGAL_CHARS = '<>:"/\\|?*'


def fs_path(p) -> str:
    """交给 OS API 的路径字符串; Windows 上对超长路径启用 \\\\?\\ 扩展前缀"""
    s = os.fspath(p)
    if not is_windows() or s.startswith("\\\\?\\"):
        return s
    try:
        ap = os.path.abspath(s)
    except OSError:
        return s
    if len(ap) < 240:
        return s
    if ap.startswith("\\\\"):          # UNC -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def surrogate_safe(s) -> str:
    """剔除文件名中无法被 UTF-8 编码的 surrogate 字符 (Windows 偶发)"""
    try:
        return str(s).encode("utf-8", "replace").decode("utf-8")
    except Exception:
        return "<unprintable>"


def _win_name_rule(name: str) -> Tuple[str, bool]:
    """Windows 文件名规则 (纯函数, 不依赖当前 OS, 便于跨平台自检)"""
    orig = name
    n = "".join("_" if (ord(c) < 32 or c in WIN_ILLEGAL_CHARS) else c
                for c in name)
    n = n.rstrip(" .")
    if n.split(".")[0].upper() in WIN_RESERVED_NAMES:
        n = "_" + n
    if not n:
        n = "_"
    return n, (n != orig)


def sanitize_windows_name(name: str) -> Tuple[str, bool]:
    """当前 OS 为 Windows 时净化文件名; 其他系统原样返回"""
    if not is_windows():
        return name, False
    return _win_name_rule(name)


def safe_join(base: Path, rel_posix: str) -> Tuple[Path, bool]:
    """把包内 POSIX 相对路径安全落到本地文件系统; 返回 (目标路径, 是否被改写)"""
    target = base
    changed = False
    for part in rel_posix.replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            continue
        new_part, ch = sanitize_windows_name(part)
        changed = changed or ch
        target = target / new_part
    return target, changed


_FS_CASE_SENSITIVE: Optional[bool] = None


def fs_case_sensitive() -> Optional[bool]:
    """文件系统是否大小写敏感 (Windows NTFS 与 macOS APFS 默认不敏感, Linux ext4 敏感).
    结果缓存在进程内, 只探测一次"""
    global _FS_CASE_SENSITIVE
    if _FS_CASE_SENSITIVE is not None:
        return _FS_CASE_SENSITIVE
    d = Path(tempfile.mkdtemp(prefix="wb_cs_"))
    try:
        a = d / ".wbcsA"
        b = d / ".wbcsa"
        write_text_lf(a, "a")
        _FS_CASE_SENSITIVE = not b.exists()
    except OSError:
        _FS_CASE_SENSITIVE = None
    finally:
        rmtree_safe(d)
    return _FS_CASE_SENSITIVE


def ci_key(p) -> str:
    """大小写不敏感文件系统上的同一性键 (Windows/macOS 均会命中)"""
    s = str(p)
    return s if fs_case_sensitive() else s.lower()


def _rmtree_onerror(func, path, exc_info) -> None:
    """Windows 只读文件/目录导致 rmtree 失败时提升权限重试"""
    try:
        os.chmod(path, 0o600)
        func(path)
    except Exception:
        pass


def rmtree_safe(path) -> None:
    try:
        shutil.rmtree(fs_path(path), ignore_errors=True,
                      onerror=_rmtree_onerror if is_windows() else None)
    except OSError:
        pass


def _read_bytes_safe(path) -> bytes:
    with open(fs_path(path), "rb") as f:
        return f.read()


def walk_fs(top) -> Iterable[Tuple[str, List[str], List[str]]]:
    """跨平台目录遍历: Windows 上自动使用 \\\\?\\ 扩展前缀并对外还原为普通路径;
    不跟随符号链接 (避免 Windows junction / macOS 环路)"""
    for root, dirs, files in os.walk(fs_path(top), followlinks=False):
        yield _strip_win_prefix(root), dirs, files


def write_text_lf(path, text: str, encoding: str = "utf-8") -> None:
    """跨平台统一以 LF 换行写入 (避免 Windows 默认把 \\n 转 \\r\\n 破坏校验值)"""
    with open(fs_path(path), "w", encoding=encoding, newline="\n") as f:
        f.write(text)


def read_text_safe(path, encoding: str = "utf-8-sig") -> str:
    with open(fs_path(path), "r", encoding=encoding, errors="replace") as f:
        return f.read()


def write_json(path, obj, indent: int = 2) -> None:
    write_text_lf(path, json.dumps(obj, ensure_ascii=False, indent=indent))


def read_json(path, default=None):
    try:
        return json.loads(read_text_safe(path))
    except (ValueError, OSError):
        return default


def disk_free(path) -> int:
    try:
        return shutil.disk_usage(fs_path(path)).free
    except OSError:
        return -1


# ------------------------------------------------------------
# WorkBuddy 版本检测 (跨平台)
# ------------------------------------------------------------

def v_tuple(v: str) -> Tuple:
    try:
        parts = re.findall(r"\d+", str(v))[:3]
        return tuple(int(x) for x in parts)
    except Exception:
        return ()


def detect_wb_version(wb_home: Optional[Path] = None) -> Tuple[Optional[str], str]:
    """返回 (版本号, 来源)。三路兜底, 任一失败不影响后续流程"""
    wb_home = wb_home or (Path.home() / ".workbuddy")
    # 1) 数据目录内的版本标记 (三平台一致, 最高优先)
    j = read_json(wb_home / "startup-seed-version.json")
    if isinstance(j, dict) and isinstance(j.get("appVersion"), str) and j["appVersion"]:
        return j["appVersion"], "startup-seed-version.json"
    if isinstance(j, dict) and isinstance(j.get("version"), str) and j["version"]:
        return j["version"], "startup-seed-version.json"
    # 2) 应用安装目录内的 Electron package.json
    for cand in _wb_app_candidates():
        j = read_json(cand)
        if isinstance(j, dict) and isinstance(j.get("version"), str) and j["version"]:
            return j["version"], str(cand)
    # 3) macOS Info.plist / Linux 包管理
    if is_macos():
        v = _read_plist_version()
        if v:
            return v, "Info.plist"
    if is_linux():
        v = _read_linux_pkg_version()
        if v:
            return v, "package-manager"
    return None, "not-detected"


def _wb_app_candidates() -> List[Path]:
    out: List[Path] = []
    home = Path.home()
    if is_windows():
        roots = [os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("PROGRAMFILES(X86)"), os.environ.get("USERPROFILE")]
        roots = [Path(r) for r in roots if r]
        for r in roots:
            for base in (r / "Programs" / "WorkBuddy",
                         r / "WorkBuddy",
                         r / "AppData" / "Local" / "WorkBuddy"):
                out.append(base / "resources" / "app" / "package.json")
                try:
                    for ch in sorted(base.iterdir()):
                        if ch.is_dir() and ch.name.startswith("app-"):
                            out.append(ch / "resources" / "app" / "package.json")
                except OSError:
                    pass
                out.append(base / "app" / "package.json")
    elif is_macos():
        out.append(Path("/Applications/WorkBuddy.app/Contents/Resources/app/package.json"))
        out.append(home / "Applications/WorkBuddy.app/Contents/Resources/app/package.json")
    else:
        for base in (Path("/opt/WorkBuddy"), Path("/opt/workbuddy"),
                     Path("/usr/share/WorkBuddy"), Path("/usr/lib/workbuddy"),
                     home / ".local/share/WorkBuddy"):
            out.append(base / "resources" / "app" / "package.json")
    return out


def _read_plist_version() -> Optional[str]:
    for app in ("/Applications/WorkBuddy.app",
                str(Path.home() / "Applications/WorkBuddy.app")):
        if not os.path.isdir(app):
            continue
        try:
            cp = subprocess.run(
                ["defaults", "read", app + "/Contents/Info.plist",
                 "CFBundleShortVersionString"],
                capture_output=True, text=True, timeout=10)
            if cp.returncode == 0 and cp.stdout.strip():
                return cp.stdout.strip()
        except Exception:
            continue
    return None


def _read_linux_pkg_version() -> Optional[str]:
    for cmd in (["rpm", "-q", "--queryformat", "%{VERSION}", "workbuddy"],
                ["dpkg-query", "-W", "-f=${Version}", "workbuddy"]):
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if cp.returncode == 0 and cp.stdout.strip():
                return cp.stdout.strip().split("-")[0]
        except Exception:
            continue
    return None


def classify_wb_version(v: Optional[str]) -> Dict[str, str]:
    """把版本号归入支持结论: supported / newer / legacy / too-old / unknown"""
    if not v:
        return {"status": "unknown", "label": "未检测到",
                "advice": "未能确定 WorkBuddy 版本, 按最严格兼容模式继续 (不额外假设目录结构)。"}
    vt = v_tuple(v)
    mn, tgt = v_tuple(WB_MIN_VERSION), v_tuple(WB_TARGET_VERSION)
    if not vt:
        return {"status": "unknown", "label": v, "advice": "版本号无法解析, 按兼容模式继续。"}
    if vt > tgt:
        return {"status": "newer", "label": v,
                "advice": "检测到高于实测版本 (%s) 的 WorkBuddy: 数据目录可能有本脚本未收录的新结构, "
                          "备份仍完整, 恢复后请人工抽查会话与技能。" % WB_TARGET_VERSION}
    if vt >= mn:
        return {"status": "supported", "label": v, "advice": "在支持范围内, 功能完整可用。"}
    if vt[:2] >= (5, 0):
        return {"status": "legacy", "label": v,
                "advice": "低于 %s: 备份可用, 恢复到新版时依赖列交集与目录自动识别, "
                          "建议用 --conflict merge 并保留目标机数据。" % WB_MIN_VERSION}
    return {"status": "too-old", "label": v,
            "advice": "版本过旧且与当前数据格式差异较大, 仅建议备份留存, 不建议直接恢复。"}


def check_app_running() -> bool:
    """WorkBuddy 主进程是否运行中 (Windows 上替换数据库会失败, 需提前提示)"""
    try:
        if is_windows():
            cp = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/NH"],
                                capture_output=True, text=True, timeout=15)
            return cp.returncode == 0 and "WorkBuddy.exe" in (cp.stdout or "")
        for pat in (["pgrep", "-x", "WorkBuddy"], ["pgrep", "-f", "WorkBuddy.app/Contents/MacOS"]):
            cp = subprocess.run(pat, capture_output=True, text=True, timeout=15)
            if cp.returncode == 0 and (cp.stdout or "").strip():
                return True
    except Exception:
        pass
    return False


def resolve_wb_home(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("WB_MIGRATE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".workbuddy"


def setup_logging(wb_home: Path, verbose: bool = False, quiet: bool = False) -> Path:
    """双输出日志: 控制台 + wb_home/logs/wb-migrate-{ts}.log"""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = wb_home / LOG_DIRNAME
    log_path = None
    handlers: List[logging.Handler] = []
    try:
        _mkdir(log_dir)
        log_path = log_dir / f"wb-migrate-{ts}.log"
        fh = logging.FileHandler(str(log_path), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        handlers.append(fh)
    except OSError:
        pass
    ch = logging.StreamHandler(_safe_stdout())
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("%(message)s"))
    handlers.append(ch)
    _LOG.setLevel(logging.DEBUG)
    _LOG.handlers.clear()
    for h in handlers:
        _LOG.addHandler(h)
    return log_path


def _safe_stdout():
    """Windows GBK 控制台保护: 强制 UTF-8 + replace"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return sys.stdout


def now_ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def now_iso() -> Tuple[str, str]:
    now = datetime.datetime.now()
    return (now.isoformat(timespec="seconds"),
            datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))


def install_signal_handlers() -> None:
    def _handler(signum, frame):
        if _INTERRUPTED["flag"]:
            _LOG.warning("收到第二次中断信号, 立即退出")
            raise InterruptedError_("user interrupt")
        _INTERRUPTED["flag"] = True
        _LOG.warning("[!] 收到中断信号, 正在安全停止 (再按一次强制退出)...")
    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):
        pass


def check_interrupt() -> None:
    if _INTERRUPTED["flag"]:
        raise InterruptedError_("user interrupt")


def register_cleanup(path: str, remove_file: bool = True) -> None:
    if path not in _CLEANUP_PATHS:
        _CLEANUP_PATHS.append(path)


def _atexit_cleanup() -> None:
    for p in _CLEANUP_PATHS:
        try:
            if os.path.isdir(p):
                rmtree_safe(p)
            elif os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


atexit.register(_atexit_cleanup)


class Progress:
    """分阶段进度输出"""

    def __init__(self, total_phases: int, title: str):
        self.total = total_phases
        self.title = title
        self.current = 0

    def phase(self, name: str) -> None:
        self.current += 1
        _LOG.info("[%d/%d] %s" % (self.current, self.total, name))

    def item(self, done: int, total: int, unit: str = "files", every: int = 200) -> None:
        if total and (done % every == 0 or done == total):
            _LOG.info("    %d/%d %s (%d%%)" % (done, total, unit, done * 100 // max(total, 1)))


def check_write_permissions(paths: List[Path]) -> None:
    for p in paths:
        if not p.exists():
            continue
        if p.is_file():
            test_target = p.parent
        else:
            test_target = p
        try:
            probe = test_target / PERMISSION_TEST_FILENAME
            with open(fs_path(probe), "w", encoding="utf-8", newline="\n") as f:
                f.write("ok")
            os.remove(fs_path(probe))
        except OSError as e:
            _LOG.error("无写入权限: %s (%s)" % (test_target, e))
            _LOG.error("若在 WorkBuddy 桌面端「安全沙箱」模式执行, 请在弹窗中点击「允许」,"
                       "或切换为「完全放开」模式后重试。")
            raise PermissionError(str(test_target))


# ============================================================
# [S2] 模块引擎: 路径规划
# ============================================================

def module_by_id(mid: str) -> Optional[Dict]:
    for m in MODULES:
        if m["id"] == mid:
            return m
    return None


def resolve_modules(selected: Optional[List[str]],
                     exclude: Optional[List[str]] = None) -> List[str]:
    """解析 --modules / --exclude-modules 为最终模块 id 列表"""
    if selected:
        for mid in selected:
            if mid not in MODULE_IDS:
                raise SystemExit("未知模块: %s (可用: %s, 运行 `modules` 命令查看)"
                                 % (mid, ",".join(MODULE_IDS)))
        result = [m for m in MODULE_IDS if m in set(selected)]
    else:
        result = list(MODULE_IDS)
    if exclude:
        for mid in exclude:
            if mid not in MODULE_IDS:
                raise SystemExit("未知模块: %s" % mid)
        result = [m for m in result if m not in set(exclude)]
    return result


def iter_module_dirs(wb_home: Path, module: Dict,
                     include_large: bool = False) -> List[Path]:
    """模块涉及的顶层目录 (含通配展开, 如 user-* / storage/user-*)"""
    out: List[Path] = []
    patterns = list(module.get("dirs", []))
    if include_large:
        patterns += list(module.get("large_dirs", []))
    for pat in patterns:
        if pat in DYNAMIC_DIR_TOKENS:
            out.extend(_expand_dynamic_dirs(wb_home, pat))
            continue
        if any(ch in pat for ch in "*?["):
            try:
                for child in sorted(wb_home.glob(pat)):
                    if child.is_dir():
                        out.append(child)
            except (OSError, ValueError):
                pass
        else:
            d = wb_home / pat
            if d.is_dir():
                out.append(d)
    return out


def _path_excluded(rel: str, extra_excludes: List[str]) -> bool:
    rel = rel.replace(os.sep, "/")
    parts = rel.split("/")
    for pat in ALWAYS_EXCLUDE_PATTERNS:
        if fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    for pat in extra_excludes:
        if pat and (fnmatch.fnmatch(rel, pat) or rel == pat
                    or any(fnmatch.fnmatch(p, pat) for p in parts)):
            return True
    return False


def _strip_win_prefix(s: str) -> str:
    """去掉 Windows \\\\?\\ 扩展前缀, 便于生成相对路径"""
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


def _expand_dynamic_dirs(wb_home: Path, token: str) -> List[Path]:
    """动态目录标记的展开规则"""
    out: List[Path] = []
    if not wb_home.is_dir():
        return out
    try:
        children = sorted(wb_home.iterdir())
    except OSError:
        return out
    for child in children:
        if not child.is_dir():
            continue
        if token == "@uuid-dirs" and UUID_DIR_RE.match(child.name):
            out.append(child)
    return out


def iter_module_files(wb_home: Path, module: Dict, opts: Dict
                      ) -> List[Tuple[Path, str, int]]:
    """返回 [(绝对路径, wb_home 内相对路径, 字节数)]"""
    out: List[Tuple[Path, str, int]] = []
    excludes: List[str] = list(opts.get("exclude_patterns", []))
    include_large = bool(opts.get("include_large"))
    include_self = bool(opts.get("include_self"))

    for d in iter_module_dirs(wb_home, module, include_large):
        for root, dirs, files in walk_fs(d):
            check_interrupt()
            root_plain = _strip_win_prefix(root)
            try:
                rel_root = os.path.relpath(root_plain, str(wb_home)).replace(os.sep, "/")
            except ValueError:
                rel_root = ""
            keep: List[str] = []
            for x in dirs:
                if _path_excluded(rel_root + "/" + x, excludes):
                    continue
                # 符号链接目录 / Windows junction: 不递归 (避免循环与越卷)
                if (Path(root_plain) / x).is_symlink():
                    continue
                keep.append(x)
            dirs[:] = keep
            for fn in sorted(files):
                # \\?\ 前缀路径已去前缀, 排除判定用 POSIX 形态
                if fn.startswith("._") or fn == ".DS_Store":
                    continue
                rel = rel_root + "/" + fn
                if _path_excluded(rel, excludes):
                    continue
                if module["id"] == "skills" and not include_self \
                        and rel.startswith("skills/wb-migrate"):
                    continue
                abs_path = Path(root_plain) / fn
                try:
                    if abs_path.is_symlink():
                        tgt = abs_path.resolve(strict=False)
                        if tgt.is_dir() or not tgt.exists():
                            continue    # 目录链接 / 断链: 不打包
                    size = abs_path.stat().st_size
                except OSError:
                    continue
                out.append((abs_path, rel, size))

    for fn in module.get("files", []):
        p = wb_home / fn
        if p.is_file() and not _path_excluded(fn, excludes):
            out.append((p, fn, p.stat().st_size))
    return out


def list_other_dirs(wb_home: Path, selected: List[str],
                    include_large: bool = False) -> List[str]:
    """未归入任何模块的顶层目录"""
    known: Set[str] = set()
    known_pats: List[str] = []
    known_pat_parents: Set[str] = set()
    for m in MODULES:
        for pat in list(m.get("dirs", [])) + list(m.get("large_dirs", [])):
            if any(ch in pat for ch in "*?["):
                known_pats.append(pat)
                known_pat_parents.add(Path(pat).parts[0])
            else:
                known.add(Path(pat).parts[0])
    others = []
    if not wb_home.is_dir():
        return others
    for child in sorted(wb_home.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        rel = name
        if name in known or name in known_pat_parents or name in ALWAYS_EXCLUDE_TOP_DIRS:
            continue
        if name in {DB_MAIN, DB_EDGE}:
            continue
        if UUID_DIR_RE.match(name):        # 旧版遗留账号根目录 -> assistant
            continue
        if any(fnmatch.fnmatch(name, p) for p in ALWAYS_EXCLUDE_PATTERNS):
            continue                       # 已在全局排除表内的机器态目录
        if any(fnmatch.fnmatch(rel, pat) for pat in known_pats):
            continue
        others.append(name)
    return others


def top_level_dir_size(p: Path) -> Tuple[int, int]:
    total, count = 0, 0
    for root, _dirs, files in walk_fs(p):
        for fn in files:
            try:
                total += os.path.join(root, fn) and os.path.getsize(os.path.join(root, fn))
                count += 1
            except OSError:
                pass
    return count, total


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f%s" % (n, unit) if unit != "B" else "%dB" % n
        n /= 1024.0
    return "%dB" % n


def cwd_to_project_dir(cwd: str) -> str:
    """cwd -> ~/.workbuddy/projects/<encoded> 目录名 (5.8.3 实测规则, 与当前 OS 无关)"""
    if not cwd:
        return ""
    norm = cwd.replace("\\", "/")
    if len(norm) >= 2 and norm[1] == ":":  # Windows 盘符路径 (无论当前 OS)
        norm = norm[0].lower() + norm[2:]
    norm = norm.lstrip("/")
    return norm.replace("/", "-")


# ============================================================
# [S4] 脱敏引擎
# ============================================================

def redact_should_omit(rel: str) -> bool:
    rel = rel.replace(os.sep, "/")
    for pat in REDACT_OMIT_PATTERNS:
        if fnmatch.fnmatch(rel, pat):
            return True
    return False


def _redact_url_query(url: str) -> str:
    if not isinstance(url, str) or ("?" not in url and "&" not in url):
        return url
    try:
        base, _, query = url.partition("?")
        if not query:
            return url
        parts = []
        for kv in query.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                if k.lower() in URL_QUERY_SENSITIVE:
                    kv = k + "=" + REDACTION_PLACEHOLDER
            parts.append(kv)
        return base + "?" + "&".join(parts)
    except Exception:
        return url


def _redact_mcp_json(obj) -> object:
    if isinstance(obj, dict):
        servers = obj.get("mcpServers")
        if isinstance(servers, dict):
            for sname, srv in servers.items():
                if not isinstance(srv, dict):
                    continue
                if isinstance(srv.get("url"), str):
                    srv["url"] = _redact_url_query(srv["url"])
                hdrs = srv.get("headers")
                if isinstance(hdrs, dict):
                    srv["headers"] = {k: REDACTION_PLACEHOLDER for k in hdrs}
                for tokkey in ("command",):
                    srv.pop(tokkey, None)
                env = srv.get("env")
                if isinstance(env, dict):
                    srv["env"] = {k: REDACTION_PLACEHOLDER for k in env}
        return _redact_json_obj(obj)
    return obj


def _strip_jsonc_line(ln: str) -> str:
    """剥离行内 // 注释 (跳过双引号字符串内的 //, 如 https://)"""
    in_str = False
    esc = False
    for i, ch in enumerate(ln):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if ch == "/" and not in_str and i + 1 < len(ln) and ln[i + 1] == "/":
            return ln[:i]
    return ln


def redact_bytes(rel: str, data: bytes) -> Tuple[bytes, bool]:
    """对文件内容做内存脱敏; 返回 (新内容, 是否发生脱敏). 解析失败按原样返回."""
    rel_norm = rel.replace(os.sep, "/")
    basename = rel_norm.rsplit("/", 1)[-1]
    if not basename.endswith(".json"):
        return data, False
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data, False
    # 兼容 JSONC (argv.json): 引号感知地剥离 // 注释
    stripped = "\n".join(_strip_jsonc_line(ln) for ln in text.splitlines())
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return data, False
    import copy
    orig = copy.deepcopy(obj)
    if basename == "mcp.json":
        new_obj = _redact_mcp_json(obj)
    else:
        new_obj = _redact_json_obj(obj)
    if new_obj == orig:
        return data, False
    out = json.dumps(new_obj, ensure_ascii=False, indent=2).encode("utf-8")
    return out, True


# ============================================================
# [S3] manifest / checksums
# ============================================================

def detect_source_home_prefix(meta: Dict) -> Optional[str]:
    """v2 兼容: 从 metadata.json 推断源 home 前缀"""
    source_os = (meta.get("platform") or {}).get("os", "")
    source_user = (meta.get("platform") or {}).get("username", "")
    source_path = meta.get("source_path", "")
    if source_path:
        sp = source_path.replace("\\", "/")
        if sp.endswith("/.workbuddy"):
            return source_path[: len(source_path) - len("/.workbuddy")]
        if "/.workbuddy" in sp:
            return source_path[: source_path.replace("\\", "/").rfind("/.workbuddy")]
    if source_os == "Darwin" and source_user:
        return "/Users/%s" % source_user
    if source_os == "Linux" and source_user:
        return "/home/%s" % source_user
    if source_os == "Windows" and source_user:
        return "C:\\Users\\%s" % source_user
    return None


def _table_row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    except sqlite3.Error:
        return -1


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def _table_pk(conn: sqlite3.Connection, table: str) -> Optional[str]:
    try:
        for r in conn.execute("PRAGMA table_info(%s)" % table):
            if r[5]:  # pk flag
                return r[1]
    except sqlite3.Error:
        pass
    return None


def _table_ddl(conn: sqlite3.Connection, table: str) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _db_table_stats(db_path: Path) -> Dict:
    stats: Dict[str, Dict] = {}
    if not db_path.is_file():
        return stats
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except sqlite3.Error:
        return stats
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            stats[t] = {"rows": _table_row_count(conn, t),
                        "columns": _table_columns(conn, t),
                        "pk": _table_pk(conn, t)}
    finally:
        conn.close()
    return stats


def _db_user_ids(db_path: Path) -> List[str]:
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except sqlite3.Error:
        return []
    try:
        if "sessions" not in _db_table_stats_cached(conn):
            return []
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM sessions WHERE user_id IS NOT NULL").fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _db_table_stats_cached(conn: sqlite3.Connection) -> Set[str]:
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def build_manifest(wb_home: Path, opts: Dict, module_stats: Dict,
                   db_info: Dict, redaction_info: Dict,
                   backup_id: str, total_files: int, total_bytes: int,
                   wb_version: Optional[str] = None,
                   wb_version_source: str = "",
                   wb_version_status: str = "unknown") -> Dict:
    ts_local, ts_utc = now_iso()
    pinfo = get_platform_info()
    main_db = db_info.get("main")
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "tool": "wb-migrate",
        "tool_version": VERSION,
        "backup_id": backup_id,
        "created_at": ts_local,
        "created_at_utc": ts_utc,
        "platform": pinfo,
        "source": {
            "wb_home": str(wb_home),
            "home_prefix": str(Path.home()),
            "user_ids": db_info.get("user_ids", []),
            "wb_version": wb_version,
            "wb_version_source": wb_version_source,
            "wb_version_status": wb_version_status,
        },
        "options": {
            "modules": opts.get("modules", list(MODULE_IDS)),
            "include_large": bool(opts.get("include_large")),
            "include_other": bool(opts.get("include_other")),
            "include_self": bool(opts.get("include_self")),
            "exclude": list(opts.get("exclude_patterns", [])),
            "redacted": bool(opts.get("redact", True)),
        },
        "modules": module_stats,
        "database": {
            "workbuddy.db": {
                "sha256": main_db.get("sha256", "") if main_db else "",
                "bytes": main_db.get("bytes", 0) if main_db else 0,
                "tables": db_info.get("table_stats", {}),
                "drizzle_included": False,
            },
        },
        "redaction": redaction_info,
        "checksums": {"algorithm": "sha256", "file": "checksums.sha256"},
        "total": {"files": total_files, "bytes": total_bytes,
                  "modules": len(module_stats)},
    }
    edge = db_info.get("edge")
    if edge:
        manifest["database"][DB_EDGE] = {
            "sha256": edge.get("sha256", ""), "bytes": edge.get("bytes", 0),
            "mode": "copy",
        }
    return manifest


def load_manifest_from_tar(tf: tarfile.TarFile) -> Dict:
    """读取 manifest.json; 旧包自动合成 legacy manifest"""
    member = None
    try:
        member = tf.getmember("manifest.json")
    except KeyError:
        member = None
    if member is not None:
        data = tf.extractfile(member).read()
        manifest = json.loads(data.decode("utf-8"))
        manifest.setdefault("legacy", False)
        return manifest

    # legacy v1/v2: 只有 metadata.json
    try:
        meta_member = tf.getmember("metadata.json")
    except KeyError:
        raise ChecksumError("备份包缺少 manifest.json 与 metadata.json, 不是有效的 wb-migrate 备份包")
    meta = json.loads(tf.extractfile(meta_member).read().decode("utf-8"))
    legacy_modules: Dict[str, Dict] = {}
    known_map: Dict[str, str] = {}
    for m in MODULES:
        for d in list(m.get("dirs", [])) + list(m.get("large_dirs", [])):
            known_map[Path(d).parts[0]] = m["id"]
        for f in m.get("files", []):
            known_map[f] = m["id"]
    known_map[DB_MAIN] = "__database__"
    known_map[DB_EDGE] = "__database__"
    top_entries = set()
    for mem in tf.getmembers():
        top_entries.add(mem.name.split("/")[0])
    unknown = []
    for top in sorted(top_entries):
        mid = known_map.get(top)
        if mid is None or mid == "__database__":
            if mid is None:
                unknown.append(top)
            continue
        legacy_modules.setdefault(mid, {"id": mid, "label": module_by_id(mid)["label"],
                                        "legacy_present": True})
    ver = str(meta.get("version", "1"))
    fmt = 2 if ver.startswith("2") else 1
    home_prefix = detect_source_home_prefix(meta)
    return {
        "format_version": fmt,
        "legacy": True,
        "tool": meta.get("tool", "wb-migrate"),
        "tool_version": ver,
        "backup_id": "legacy-%s" % now_ts(),
        "created_at": meta.get("timestamp", ""),
        "platform": meta.get("platform", {}),
        "source": {"wb_home": meta.get("source_path", ""),
                   "home_prefix": home_prefix or "",
                   "user_ids": []},
        "options": {"modules": sorted(legacy_modules.keys()),
                    "redacted": None},
        "modules": legacy_modules,
        "database": {},
        "redaction": {"enabled": None},
        "checksums": None,
        "unknown_top_entries": unknown,
    }


def _check_container_integrity(archive: Path) -> Optional[str]:
    """解压整个 gzip 容器, 捕获截断/CRC 损坏 (Windows 复制中断、网盘未同步完常见)"""
    try:
        seen = 0
        with open(fs_path(archive), "rb") as f:
            with gzip.GzipFile(fileobj=f, mode="rb") as gz:
                while True:
                    chunk = gz.read(1 << 20)
                    if not chunk:
                        break
                    seen += len(chunk)
        if seen == 0:
            return "备份包解压后为空 (0 字节)"
    except (OSError, EOFError, zlib.error) as e:
        return "备份包容器损坏: %s %s (可能传输中断/写入不完整)" \
               % (type(e).__name__, str(e) or "")
    except Exception as e:                                   # noqa: BLE001
        return "备份包不可读: %s: %s" % (type(e).__name__, e)
    return None


def verify_archive(archive: Path) -> Dict:
    """流式校验: 容器完整性 -> manifest -> checksums.sha256 -> 逐文件 sha256"""
    errors: List[str] = []
    cont_err = _check_container_integrity(archive)
    if cont_err:
        return {"ok": False, "errors": [cont_err], "manifest": {}}
    with tarfile.open(fs_path(archive), "r:gz") as tf:
        members = tf.getmembers()
        if not members:
            errors.append("备份包内没有任何条目")
        else:
            tops = set(m.name.split("/")[0] for m in members)
            VALID_TOPS = {"manifest.json", "metadata.json", "checksums.sha256",
                          "modules", "database"}
            if not (tops & VALID_TOPS):
                errors.append("不是有效的 wb-migrate 备份包 (顶层条目: %s)"
                              % ",".join(sorted(tops)[:6]))
        manifest = load_manifest_from_tar(tf)
        cs_info = manifest.get("checksums")
        if not cs_info:
            _LOG.warning("旧格式备份包 (v%d), 无校验清单, 仅做可读性检查" % manifest.get("format_version", 1))
            for m in tf.getmembers():
                if m.isfile():
                    try:
                        tf.extractfile(m).read()
                    except Exception as e:
                        errors.append("成员读取失败: %s (%s)" % (m.name, e))
            return {"ok": not errors, "errors": errors, "manifest": manifest}

        try:
            cs_member = tf.getmember("checksums.sha256")
        except KeyError:
            raise ChecksumError("备份包缺少 checksums.sha256")
        cs_bytes = tf.extractfile(cs_member).read()
        digest = hashlib.sha256(cs_bytes).hexdigest()
        expect_digest = cs_info.get("digest")
        if expect_digest and digest != expect_digest:
            raise ChecksumError("checksums.sha256 清单摘要不匹配 (期望 %s, 实际 %s)"
                                % (expect_digest[:12], digest[:12]))
        expected: Dict[str, str] = {}
        for line in cs_bytes.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                expected[parts[1].lstrip("*")] = parts[0]
        checked = 0
        for m in tf.getmembers():
            if not m.isfile():
                continue
            if m.name in ("manifest.json", "checksums.sha256", "metadata.json"):
                continue
            if m.name not in expected:
                errors.append("未在清单中的文件: %s" % m.name)
                continue
            h = hashlib.sha256()
            fobj = tf.extractfile(m)
            while True:
                chunk = fobj.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
            if h.hexdigest() != expected[m.name]:
                errors.append("sha256 不匹配: %s" % m.name)
            checked += 1
        missing = set(expected) - {m.name for m in tf.getmembers()}
        for name in sorted(missing):
            errors.append("清单中存在但包内缺失: %s" % name)
        _LOG.debug("校验 %d 个文件, %d 个错误" % (checked, len(errors)))
    return {"ok": not errors, "errors": errors, "manifest": manifest}


# ============================================================
# 安全解包 (兼容 Python 3.9)
# ============================================================

def _validate_tar_member(member: tarfile.TarInfo, dest: Path) -> None:
    name_norm = member.name.replace("\\", "/")
    if member.name.startswith(("/", "\\")) or os.path.isabs(member.name):
        raise ChecksumError("非法 tar 成员路径: %s" % member.name)
    if ".." in name_norm.split("/"):
        raise ChecksumError("非法 tar 成员路径: %s" % member.name)
    if PureWindowsPath(member.name).is_absolute():
        raise ChecksumError("非法 Windows 绝对路径: %s" % member.name)
    if member.issym() or member.islnk():
        raise ChecksumError("不允许符号链接成员: %s" % member.name)
    if member.ischr() or member.isblk() or member.isfifo():
        raise ChecksumError("不允许设备成员: %s" % member.name)
    target, _changed = safe_join(dest, member.name)
    if not is_windows():
        tgt_s = str(target.resolve())
        dest_s = str(dest.resolve())
        if not (tgt_s.startswith(dest_s) or tgt_s.lower().startswith(dest_s.lower())):
            raise ChecksumError("路径越界: %s" % member.name)


def safe_extract_tar(archive: Path, dest: Path) -> None:
    _mkdir(dest)
    seen: Set[str] = set()
    with tarfile.open(fs_path(archive), "r:gz") as tf:
        for member in tf.getmembers():
            _validate_tar_member(member, dest)
        for member in tf.getmembers():
            target, changed = safe_join(dest, member.name)
            if changed:
                _LOG.warning("文件名已按 %s 规则净化: %s -> %s"
                             % (platform.system(), surrogate_safe(member.name), target.name))
            key = ci_key(target)
            if member.isdir():
                _mkdir(target)
                seen.add(key)
            elif member.isfile():
                if key in seen:
                    # 大小写不敏感文件系统上的同名冲突 (源自 macOS/Linux 的包)
                    target = target.parent / (target.name + ".imported-%s" % now_ts())
                    key = ci_key(target)
                    _LOG.warning("大小写冲突: %s -> %s" % (member.name, target.name))
                _mkdir(target.parent)
                fsrc = tf.extractfile(member)
                tmp = target.parent / (target.name + ".part")
                with open(fs_path(tmp), "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
                os.replace(fs_path(tmp), fs_path(target))
                seen.add(key)
            else:
                _LOG.warning("跳过非常规成员: %s" % member.name)


# ============================================================
# [S5] 备份引擎
# ============================================================

def _copy_sqlite_snapshot(src_db: Path, dest: Path) -> bool:
    """SQLite backup API 一致性快照 (含 WAL 未 checkpoint 数据)"""
    _mkdir(dest.parent)
    try:
        src = sqlite3.connect("file:%s?mode=ro" % src_db, uri=True)
    except sqlite3.Error:
        return False
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
        return True
    except sqlite3.Error:
        return False
    finally:
        src.close()


def _tar_add_bytes(tf: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o644
    tf.addfile(info, io.BytesIO(data))


def _tar_add_file(tf: tarfile.TarFile, abs_path: Path, arcname: str,
                  redact: bool) -> Tuple[str, int, bool]:
    """添加单个文件, 返回 (sha256, size, redacted). 脱敏在内存中完成, 不改源文件."""
    data = _read_bytes_safe(abs_path)
    redacted = False
    if redact:
        rel_for_redact = arcname
        data, redacted = redact_bytes(rel_for_redact, data)
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    try:
        info.mtime = int(os.stat(fs_path(abs_path)).st_mtime)
    except OSError:
        info.mtime = int(time.time())
    info.mode = 0o644
    tf.addfile(info, io.BytesIO(data))
    return hashlib.sha256(data).hexdigest(), len(data), redacted


def _snapshot_databases(wb_home: Path, staging: Path) -> Dict:
    """快照主库与 edge 库到 staging/database/, 返回 db_info"""
    db_info: Dict = {}
    main_src = wb_home / DB_MAIN
    main_snap = staging / "database" / DB_MAIN
    ok = False
    if os.path.isfile(fs_path(main_src)):
        ok = _copy_sqlite_snapshot(main_src, main_snap)
        if not ok:
            shutil.copy2(fs_path(main_src), fs_path(main_snap))
            ok = True
    if ok:
        data = _read_bytes_safe(main_snap)
        db_info["main"] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
        db_info["table_stats"] = _db_table_stats(main_snap)
        db_info["user_ids"] = _db_user_ids(main_snap)
    else:
        db_info["main"] = {"sha256": "", "bytes": 0}
        db_info["table_stats"] = {}
        db_info["user_ids"] = []

    edge_src = wb_home / DB_EDGE
    if os.path.isfile(fs_path(edge_src)):
        edge_snap = staging / "database" / DB_EDGE
        if not _copy_sqlite_snapshot(edge_src, edge_snap):
            shutil.copy2(fs_path(edge_src), fs_path(edge_snap))
        data = _read_bytes_safe(edge_snap)
        db_info["edge"] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
    return db_info


def create_backup(opts: Dict) -> Optional[Path]:
    """创建 v3 备份包 (staging tar -> 分模块 -> checksums -> 原子 gzip)"""
    wb_home: Path = opts["wb_home"]
    if not wb_home.is_dir():
        raise FileNotFoundError("WorkBuddy 数据目录不存在: %s" % wb_home)
    output_dir: Path = opts.get("output") or Path.cwd()
    _mkdir(output_dir)
    check_write_permissions([wb_home, output_dir])

    # 版本检测 (写入清单 -> 恢复端据此给出跨版本提示)
    wb_ver, wb_src = detect_wb_version(wb_home)
    cls = classify_wb_version(wb_ver)
    if cls["status"] == "too-old":
        _LOG.error("[版本不受支持] WorkBuddy %s: %s" % (cls["label"], cls["advice"]))
    elif cls["status"] != "supported":
        _LOG.warning("[版本提示] WorkBuddy %s: %s" % (cls["label"], cls["advice"]))
    if check_app_running():
        _LOG.warning("[提示] WorkBuddy 正在运行, 备份为一致性快照 (可读), "
                     "但请勿在此时对新机器执行恢复。")

    prog = Progress(6, "backup")
    resume_id = opts.get("resume")
    if resume_id:
        staging_root = wb_home / STAGING_DIRNAME / resume_id
        if not staging_root.is_dir():
            raise FileNotFoundError("未找到可续传的备份任务: %s" % resume_id)
        backup_id = resume_id
        state = _load_state(staging_root)
        _LOG.info("[续传] 从断点继续备份 %s (已完成模块: %s)"
                  % (backup_id, ",".join(state.get("modules_done", [])) or "无"))
    else:
        backup_id = "%s-%04d" % (now_ts(), random.randint(0, 9999))
        staging_root = wb_home / STAGING_DIRNAME / backup_id
        if staging_root.exists():
            rmtree_safe(staging_root)
        _mkdir(staging_root)
        state = {"backup_id": backup_id, "modules_done": [],
                 "db_done": False, "ts": now_iso()[0]}

    selected = opts["modules"]
    include_large = bool(opts.get("include_large"))
    include_other = bool(opts.get("include_other"))
    include_self = bool(opts.get("include_self"))
    redact = bool(opts.get("redact", True))
    exclude_patterns = list(opts.get("exclude_patterns", []))
    dry_run = bool(opts.get("dry_run"))

    plan_opts = {"exclude_patterns": exclude_patterns,
                 "include_large": include_large, "include_self": include_self}

    # ---- 规划 ----
    prog.phase("预检与模块规划")
    module_plans: Dict[str, List[Tuple[Path, str, int]]] = {}
    for mid in selected:
        mod = module_by_id(mid)
        files = iter_module_files(wb_home, mod, plan_opts)
        module_plans[mid] = files
    other_dirs: List[str] = []
    if include_other and "other" not in selected:
        other_dirs = list_other_dirs(wb_home, selected, include_large)

    est_total = sum(len(v) for v in module_plans.values())
    est_bytes = sum(f[2] for v in module_plans.values() for f in v)
    _LOG.info("模块: %s%s" % (",".join(selected),
                              (" + other(%s)" % ",".join(other_dirs)) if other_dirs else ""))
    _LOG.info("预计打包 %d 个文件, 约 %s" % (est_total, human_size(est_bytes)))
    # 磁盘空间检查 (输出 + staging 双份)
    try:
        free = shutil.disk_usage(str(output_dir)).free
        if est_bytes * 3 > free:
            _LOG.warning("磁盘剩余空间 %s 偏小, 备份可能失败" % human_size(free))
    except OSError:
        pass
    if dry_run:
        _LOG.info("[DRY-RUN] 仅预览, 未产生备份包")
        for mid, files in module_plans.items():
            _LOG.info("  %-14s %5d 文件  %s" % (mid, len(files),
                                                human_size(sum(f[2] for f in files))))
        return None

    # ---- staging payload ----
    prog.phase("数据库一致性快照")
    payload_tar = staging_root / "payload.tar"
    tf = tarfile.open(fs_path(payload_tar), "a", format=tarfile.PAX_FORMAT)
    try:
        if not state.get("db_done"):
            db_info = _snapshot_databases(wb_home, staging_root)
            if db_info.get("main", {}).get("bytes"):
                data = (staging_root / "database" / DB_MAIN).read_bytes()
                _tar_add_bytes(tf, "database/%s" % DB_MAIN, data)
            if db_info.get("edge"):
                data = (staging_root / "database" / DB_EDGE).read_bytes()
                _tar_add_bytes(tf, "database/%s" % DB_EDGE, data)
            state["db_done"] = True
            state["db_info"] = {k: db_info[k] for k in ("table_stats", "user_ids")}
            # 主/edge 库的 sha256 记录在 state 中
            state["db_sha"] = {
                "main": db_info.get("main", {}).get("sha256", ""),
                "main_bytes": db_info.get("main", {}).get("bytes", 0),
                "edge": db_info.get("edge", {}).get("sha256", ""),
                "edge_bytes": db_info.get("edge", {}).get("bytes", 0),
            }
            _save_state(staging_root, state)
        else:
            db_info = dict(state.get("db_info", {}))
            db_info.update(state.get("db_sha", {}))
            db_info["main"] = {"sha256": state.get("db_sha", {}).get("main", ""),
                               "bytes": state.get("db_sha", {}).get("main_bytes", 0)}
            if state.get("db_sha", {}).get("edge"):
                db_info["edge"] = {"sha256": state["db_sha"]["edge"],
                                   "bytes": state["db_sha"].get("edge_bytes", 0)}
            if not db_info.get("table_stats"):
                db_info["table_stats"] = {}
            if not db_info.get("user_ids"):
                db_info["user_ids"] = []

        # ---- 分模块打包 ----
        redaction_info = {"enabled": redact, "rules_version": REDACTION_RULES_VERSION,
                          "placeholder": REDACTION_PLACEHOLDER,
                          "redacted_files": [], "omitted_files": []}
        module_stats: Dict[str, Dict] = {}
        total_files, total_bytes = 0, 0
        n_modules = len(selected) + (1 if other_dirs else 0)
        done_mods = 0
        for mid in selected:
            check_interrupt()
            if mid in state.get("modules_done", []):
                _LOG.info("  [跳过已完成] %s" % mid)
                continue
            files = module_plans[mid]
            _LOG.info("    打包模块 %s (%d/%d): %d 文件"
                      % (mid, done_mods + 1, n_modules, len(files)))
            mod_count, mod_bytes = 0, 0
            for i, (abs_path, rel, size) in enumerate(files):
                check_interrupt()
                if redact and redact_should_omit(rel):
                    redaction_info["omitted_files"].append(rel)
                    continue
                sha, sz, was_redacted = _tar_add_file(
                    tf, abs_path, "modules/%s/%s" % (mid, rel), redact)
                if was_redacted:
                    redaction_info["redacted_files"].append(rel)
                mod_count += 1
                mod_bytes += sz
                if i % 200 == 0 or i + 1 == len(files):
                    _LOG.info("    %d/%d files" % (i + 1, len(files)))
            module_stats[mid] = {"id": mid,
                                 "label": module_by_id(mid)["label"],
                                 "files_count": mod_count, "bytes": mod_bytes}
            total_files += mod_count
            total_bytes += mod_bytes
            state["modules_done"].append(mid)
            _save_state(staging_root, state)
            done_mods += 1

        if other_dirs:
            _LOG.info("    打包 other 模块 (%d/%d)" % (done_mods + 1, n_modules))
            other_count, other_bytes = 0, 0
            for dname in other_dirs:
                droot = wb_home / dname
                for root, dirs, files in walk_fs(droot):
                    check_interrupt()
                    rel_root = os.path.relpath(root, str(wb_home)).replace(os.sep, "/")
                    dirs[:] = [x for x in dirs
                               if not _path_excluded(rel_root + "/" + x, exclude_patterns)]
                    for fn in sorted(files):
                        rel = rel_root + "/" + fn
                        if _path_excluded(rel, exclude_patterns):
                            continue
                        if redact and redact_should_omit(rel):
                            redaction_info["omitted_files"].append(rel)
                            continue
                        sha, sz, was_red = _tar_add_file(
                            tf, Path(root) / fn, "modules/other/%s" % rel, redact)
                        if was_red:
                            redaction_info["redacted_files"].append(rel)
                        other_count += 1
                        other_bytes += sz
            module_stats["other"] = {"id": "other", "label": "未归类",
                                     "files_count": other_count, "bytes": other_bytes}
            total_files += other_count
            total_bytes += other_bytes
    finally:
        tf.close()

    # ---- checksums + manifest ----
    prog.phase("生成校验清单")
    checksum_lines: List[str] = []
    with tarfile.open(fs_path(payload_tar), "r:") as tfc:
        for m in tfc.getmembers():
            if not m.isfile():
                continue
            if m.name in ("manifest.json", "checksums.sha256", "metadata.json"):
                continue
            h = hashlib.sha256()
            fobj = tfc.extractfile(m)
            while True:
                chunk = fobj.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
            checksum_lines.append("%s  %s" % (h.hexdigest(), m.name))
    cs_bytes = ("\n".join(checksum_lines) + "\n").encode("utf-8")
    cs_digest = hashlib.sha256(cs_bytes).hexdigest()

    manifest = build_manifest(wb_home, opts, module_stats, db_info, redaction_info,
                              backup_id, total_files, total_bytes,
                              wb_ver, wb_src, cls["status"])
    manifest["checksums"]["digest"] = cs_digest
    meta_v2 = {
        "version": VERSION, "tool": "wb-migrate",
        "timestamp": manifest["created_at"],
        "timestamp_utc": manifest["created_at_utc"],
        "platform": manifest["platform"],
        "backup_type": "full" if include_large else "standard",
        "source_path": str(wb_home),
    }

    tf = tarfile.open(fs_path(payload_tar), "a", format=tarfile.PAX_FORMAT)
    try:
        _tar_add_bytes(tf, "checksums.sha256", cs_bytes)
        _tar_add_bytes(tf, "manifest.json",
                       json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        _tar_add_bytes(tf, "metadata.json",
                       json.dumps(meta_v2, ensure_ascii=False, indent=2).encode("utf-8"))
    finally:
        tf.close()

    # ---- 原子 gzip 落盘 ----
    prog.phase("压缩落盘")
    username = manifest["platform"]["username"]
    hostname = manifest["platform"]["hostname"]
    final_path = output_dir / ("wb-backup-%s-%s-%s.tar.gz" % (username, hostname, now_ts()))
    part_path = final_path.with_suffix(final_path.suffix + ".part")
    with open(fs_path(payload_tar), "rb") as fi, gzip.open(fs_path(part_path), "wb", compresslevel=int(opts.get("level", 6))) as fo:
        shutil.copyfileobj(fi, fo, 1024 * 1024)
    os.replace(fs_path(part_path), fs_path(final_path))

    prog.phase("完成")
    rmtree_safe(staging_root)
    _LOG.info("备份完成: %s (%s, %d 文件)"
              % (final_path, human_size(final_path.stat().st_size), total_files))
    if redaction_info["redacted_files"] or redaction_info["omitted_files"]:
        _LOG.info("[安全] 已脱敏 %d 个文件, 省略 %d 个敏感文件 (凭据不可还原, 目标端需重新授权)"
                  % (len(redaction_info["redacted_files"]),
                     len(redaction_info["omitted_files"])))
    _LOG.info("建议运行: wb-migrate info %s --verify" % final_path)
    return final_path


def _save_state(staging_root: Path, state: Dict) -> None:
    write_json(staging_root / "state.json", state)


def _load_state(staging_root: Path) -> Dict:
    p = staging_root / "state.json"
    if p.is_file():
        return read_json(p)
    return {}


# ============================================================
# scan / modules / info 命令
# ============================================================

def cmd_scan(opts: Dict) -> None:
    wb_home: Path = opts["wb_home"]
    pinfo = get_platform_info()
    _LOG.info("WorkBuddy 数据目录: %s" % wb_home)
    _LOG.info("平台: %s %s (%s) / Python %s"
              % (pinfo["os"], pinfo["os_release"], pinfo["machine"], pinfo["python_version"]))
    wb_ver, wb_src = detect_wb_version(wb_home)
    cls = classify_wb_version(wb_ver)
    _LOG.info("WorkBuddy 版本: %s  (%s, 来源 %s)"
              % (cls["label"], cls["status"], wb_src))
    if cls["status"] in ("newer", "legacy", "too-old", "unknown"):
        _LOG.warning("[兼容提示] %s" % cls["advice"])
    if check_app_running():
        _LOG.warning("[提示] WorkBuddy 正在运行: 备份可读, 但恢复替换数据库会失败, 请先退出应用。")
    if not wb_home.is_dir():
        _LOG.error("目录不存在")
        return

    main_db = wb_home / DB_MAIN
    if main_db.is_file():
        stats = _db_table_stats(main_db)
        _LOG.info("数据库: %s (%s)" % (DB_MAIN, human_size(main_db.stat().st_size)))
        for t, pk, _mod in DB_TABLES:
            if t in stats:
                _LOG.info("    %-32s %6d 行  (pk: %s)" % (t, stats[t]["rows"], pk))
        if "__workbuddy_drizzle_migrations" in stats:
            _LOG.info("    %-32s %6d 行  (跳过合并)" % ("__workbuddy_drizzle_migrations",
                                                      stats["__workbuddy_drizzle_migrations"]["rows"]))
        uids = _db_user_ids(main_db)
        if uids:
            _LOG.info("    账号 user_id: %s" % ", ".join(uids))
    else:
        _LOG.warning("数据库不存在: %s" % main_db)

    edge_db = wb_home / DB_EDGE
    if edge_db.is_file():
        _LOG.info("同步映射库: %s (%s)" % (DB_EDGE, human_size(edge_db.stat().st_size)))

    include_large = bool(opts.get("include_large"))
    plan_opts = {"exclude_patterns": [], "include_large": include_large,
                 "include_self": bool(opts.get("include_self"))}
    selected = opts.get("modules") or MODULE_IDS
    _LOG.info("")
    _LOG.info("模块数据 (选中 %d 个):" % len(selected))
    grand_files, grand_bytes = 0, 0
    for mid in selected:
        mod = module_by_id(mid)
        files = iter_module_files(wb_home, mod, plan_opts)
        nb = sum(f[2] for f in files)
        grand_files += len(files)
        grand_bytes += nb
        tables = mod.get("tables", [])
        _LOG.info("  %-14s %-6s %5d 文件  %10s  %s"
                  % (mid, mod["label"], len(files), human_size(nb),
                     ("表: " + ",".join(tables)) if tables else ""))
    _LOG.info("  合计: %d 文件, %s" % (grand_files, human_size(grand_bytes)))

    _LOG.info("")
    _LOG.info("默认排除的大目录:")
    for d in ("binaries", "plugins", "logs", "skills-marketplace", "traces", "app"):
        p = wb_home / d
        if p.is_dir():
            count, size = top_level_dir_size(p)
            _LOG.info("  %-20s %5d 文件  %10s" % (d, count, human_size(size)))
    others = list_other_dirs(wb_home, MODULE_IDS)
    if others:
        _LOG.info("未归类目录 (--include-other 可纳入): %s" % ",".join(others))


def cmd_modules(as_json: bool = False) -> None:
    rows = []
    for m in MODULES:
        rows.append({
            "id": m["id"], "label": m["label"],
            "dirs": m.get("dirs", []),
            "large_dirs": m.get("large_dirs", []),
            "files": m.get("files", []),
            "tables": m.get("tables", []),
        })
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    _LOG.info("可用模块 (--modules 传 id, 逗号分隔):")
    for r in rows:
        parts = []
        if r["dirs"]:
            parts.append("目录: " + ",".join(r["dirs"]))
        if r["large_dirs"]:
            parts.append("大目录(--include-large): " + ",".join(r["large_dirs"]))
        if r["files"]:
            parts.append("文件: " + ",".join(r["files"]))
        if r["tables"]:
            parts.append("表: " + ",".join(r["tables"]))
        _LOG.info("  %-14s %-6s %s" % (r["id"], r["label"], "; ".join(parts)))


def cmd_info(opts: Dict) -> int:
    archive: Path = opts["archive"]
    if not archive.is_file():
        _LOG.error("备份包不存在: %s" % archive)
        return EXIT_ERROR
    with tarfile.open(fs_path(archive), "r:gz") as tf:
        manifest = load_manifest_from_tar(tf)
        n_members = len(tf.getmembers())
        size = archive.stat().st_size

    legacy = manifest.get("legacy", False)
    _LOG.info("备份包: %s (%s, %d 个成员)" % (archive, human_size(size), n_members))
    _LOG.info("格式版本: %d%s  工具: %s %s" % (
        manifest.get("format_version", 0),
        " (旧格式, 兼容读取)" if legacy else "",
        manifest.get("tool", "?"), manifest.get("tool_version", "?")))
    _LOG.info("创建时间: %s" % manifest.get("created_at", "?"))
    src = manifest.get("source", {})
    _LOG.info("源 WorkBuddy: %s" % (src.get("wb_version") or "未知 (旧包)"))
    _LOG.info("源平台: %s  用户: %s  主机: %s" % (
        (manifest.get("platform") or {}).get("os", "?"),
        (manifest.get("platform") or {}).get("username", "?"),
        (manifest.get("platform") or {}).get("hostname", "?")))
    _LOG.info("源 home 前缀: %s" % src.get("home_prefix", "?"))
    if src.get("user_ids"):
        _LOG.info("源账号: %s" % ", ".join(src["user_ids"]))

    red = manifest.get("redaction") or {}
    if red.get("enabled") is True:
        _LOG.info("[安全] 此备份包已脱敏: 凭据不可还原, 连接器在目标端需重新授权")
        omitted = red.get("omitted_files") or []
        if omitted:
            _LOG.info("    省略的敏感文件: %d 个 (如 %s)" % (len(omitted), omitted[:3]))
    elif red.get("enabled") is None:
        _LOG.warning("[注意] 旧格式备份包, 可能包含明文凭据, 分享前请检查")

    mods = manifest.get("modules") or {}
    if mods:
        _LOG.info("包含模块 (%d):" % len(mods))
        for mid, info in mods.items():
            if isinstance(info, dict) and "files_count" in info:
                _LOG.info("  %-14s %6d 文件  %s" % (mid, info.get("files_count", 0),
                                                     human_size(info.get("bytes", 0))))
            else:
                _LOG.info("  %-14s (存在)" % mid)
    db_info = manifest.get("database") or {}
    if db_info.get("workbuddy.db", {}).get("bytes"):
        _LOG.info("数据库快照: %s (%s)" % (
            human_size(db_info["workbuddy.db"]["bytes"]),
            "含 edge-sync 库" if DB_EDGE in db_info else "仅主库"))

    cur_os = platform.system()
    src_os = (manifest.get("platform") or {}).get("os", "")
    if src_os and src_os != cur_os:
        _LOG.info("[跨平台] 源 %s -> 目标 %s, 恢复时建议加 --auto-map" % (src_os, cur_os))
    elif legacy:
        _LOG.info("旧格式包恢复时建议加 --auto-map 以处理账号合并")

    if opts.get("verify"):
        _LOG.info("")
        _LOG.info("校验中...")
        result = verify_archive(archive)
        if result["ok"]:
            _LOG.info("[OK] 完整性校验通过")
            return EXIT_OK
        for e in result["errors"][:20]:
            _LOG.error("  [FAIL] %s" % e)
        _LOG.error("校验失败: %d 个错误" % len(result["errors"]))
        return EXIT_CHECKSUM
    return EXIT_OK


# ============================================================
# [S8] 回滚日志 (RollbackJournal)
# ============================================================

class RollbackJournal:
    """记录每次 restore 的操作, 供 rollback 倒序撤销"""

    def __init__(self, history_dir: Path, archive: Path, manifest: Dict,
                 conflict: str, modules: List[str]):
        self.dir = history_dir
        self.path = history_dir / "rollback.json"
        self.entries: List[Dict] = []
        self.meta = {
            "rollback_id": history_dir.name,
            "schema": 1,
            "created_at": now_iso()[0],
            "archive": {"path": str(archive)},
            "tool_version": VERSION,
            "conflict": conflict,
            "modules": modules,
            "format_version": manifest.get("format_version", 0),
            "operations": self.entries,
        }
        if conflict == "merge":
            # merge 模式下数据库只新增不修改, 文件只新增不覆盖
            pass

    def add(self, op_type: str, **kw) -> None:
        kw["seq"] = len(self.entries) + 1
        kw["type"] = op_type
        self.entries.append(kw)

    def save(self, db_backup: Optional[Path] = None) -> Path:
        _mkdir(self.dir)
        if db_backup is not None:
            self.meta["db_snapshot"] = str(db_backup)
        write_json(self.path, self.meta)
        return self.path

    # ---- 便捷记录 ----
    def file_created(self, rel: str, sha: str) -> None:
        self.add("file_created", path=rel, sha256=sha)

    def file_overwritten(self, rel: str, backup_rel: str, sha_before: str) -> None:
        self.add("file_overwritten", path=rel, backup=backup_rel, sha256_before=sha_before)

    def db_rows_inserted(self, table: str, pks: List[str], row_shas: Dict[str, str]) -> None:
        self.add("db_rows_inserted", table=table, pks=pks, row_sha256=row_shas)

    def db_row_overwritten(self, table: str, pk: str, old_values: Dict[str, object]) -> None:
        self.add("db_row_overwritten", table=table, pk=pk, old=old_values)

    def db_row_renamed(self, table: str, old_pk: str, new_pk: str, row_sha: str) -> None:
        self.add("db_row_renamed", table=table, old_pk=old_pk, new_pk=new_pk,
                 row_sha256=row_sha)

    def db_rows_updated(self, table: str, column: str,
                        pairs: List[List[object]]) -> None:
        self.add("db_rows_updated", table=table, column=column, rows=pairs)

    def dir_renamed(self, frm: str, to: str) -> None:
        self.add("dir_renamed", frm=frm, to=to)

    def dir_created(self, rel: str) -> None:
        self.add("dir_created", path=rel)

    def dir_merged(self, dest: str, moved: List[str], leftover: str) -> None:
        self.add("dir_merged", dest=dest, moved=moved, leftover=leftover)

    def file_moved(self, frm: str, to: str) -> None:
        self.add("file_moved", frm=frm, to=to)

    def db_rows_deleted(self, table: str, rows: List[Dict]) -> None:
        """整行删除的可回滚记录 (账号 stash 用)"""
        self.add("db_rows_deleted", table=table, rows=rows)

    def shared_file_written(self, rel: str, prev_sha: str, prev_rel: str) -> None:
        """共享配置文件被改写前的记录"""
        self.add("file_overwritten", path=rel, backup=prev_rel, sha256_before=prev_sha)


def history_root(wb_home: Path) -> Path:
    return wb_home / HISTORY_DIRNAME


def list_rollbacks(wb_home: Path) -> List[Dict]:
    root = history_root(wb_home)
    out = []
    if not root.is_dir():
        return out
    # 按回滚日志文件的修改时间排序 (新->旧), 避免同名秒级目录的字典序歧义
    def _mtime(p: Path) -> float:
        try:
            return (p / "rollback.json").stat().st_mtime
        except OSError:
            return 0.0
    for child in sorted([c for c in root.iterdir() if c.is_dir()],
                        key=_mtime, reverse=True):
        rj = child / "rollback.json"
        if rj.is_file():
            try:
                data = read_json(rj)
                out.append(data)
            except (OSError, json.JSONDecodeError):
                continue
    return out


def cmd_rollback(opts: Dict) -> int:
    wb_home: Path = opts["wb_home"]
    root = history_root(wb_home)

    if opts.get("prune"):
        keep = int(opts.get("keep", 5))
        entries = []
        if root.is_dir():
            def _mtime(p: Path) -> float:
                try:
                    return (p / "rollback.json").stat().st_mtime
                except OSError:
                    return p.stat().st_mtime
            entries = sorted([c for c in root.iterdir() if c.is_dir()],
                             key=_mtime, reverse=True)
        removed = 0
        for child in entries[keep:]:
            rmtree_safe(child)
            removed += 1
        _LOG.info("已清理 %d 个历史快照, 保留最近 %d 个" % (removed, keep))
        return EXIT_OK

    if opts.get("list"):
        entries = list_rollbacks(wb_home)
        if not entries:
            _LOG.info("暂无可回滚的恢复记录 (%s)" % root)
            return EXIT_OK
        _LOG.info("可回滚记录 (新->旧):")
        for e in entries[:20]:
            _LOG.info("  %s  %s  conflict=%s  modules=%s  操作 %d 条"
                      % (e.get("rollback_id", "?"), e.get("created_at", "?"),
                         e.get("conflict", "?"), ",".join(e.get("modules", [])),
                         len(e.get("operations", []))))
        return EXIT_OK

    target: Optional[Dict] = None
    if opts.get("id"):
        for e in list_rollbacks(wb_home):
            if e.get("rollback_id") == opts["id"]:
                target = e
                break
        if target is None:
            _LOG.error("未找到回滚记录: %s" % opts["id"])
            return EXIT_ERROR
    else:
        entries = list_rollbacks(wb_home)
        if not entries:
            _LOG.error("暂无可回滚的恢复记录")
            return EXIT_ERROR
        target = entries[0]

    rid = target["rollback_id"]
    jdir = root / rid
    dry = bool(opts.get("dry_run"))
    _LOG.info("回滚 %s (%d 条操作)%s"
              % (rid, len(target.get("operations", [])),
                 " [DRY-RUN]" if dry else ""))

    if opts.get("hard"):
        snap = target.get("db_snapshot")
        if not snap or not os.path.isfile(fs_path(snap)):
            _LOG.error("硬回滚需要数据库快照, 未找到: %s" % snap)
            return EXIT_ERROR
        tgt_db = wb_home / DB_MAIN
        if dry:
            _LOG.info("[DRY-RUN] 将用快照整体替换 %s" % tgt_db)
        else:
            shutil.copy2(fs_path(snap), fs_path(tgt_db))
            _LOG.info("已用快照替换数据库 (文件层改动未撤销)")
        return EXIT_OK

    ops = list(target.get("operations", []))
    done, skipped = 0, 0
    db_path = wb_home / DB_MAIN
    conn = None
    if os.path.isfile(fs_path(db_path)) and not dry:
        ensure_writable(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.isolation_level = None

    def _file_sha(p: Path) -> Optional[str]:
        try:
            h = hashlib.sha256()
            with open(fs_path(p), "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    try:
        for op in reversed(ops):
            t = op.get("type")
            try:
                if t == "file_created":
                    p, _c = safe_join(wb_home, op["path"])
                    if not os.path.isfile(fs_path(p)):
                        skipped += 1
                        continue
                    cur = _file_sha(p)
                    if op.get("sha256") and cur != op["sha256"]:
                        _LOG.warning("[SKIP] %s 内容已变化, 不删除" % op["path"])
                        skipped += 1
                        continue
                    if not dry:
                        ensure_writable(p)
                        os.remove(fs_path(p))
                    done += 1
                elif t == "file_overwritten":
                    p, _c = safe_join(wb_home, op["path"])
                    bak = jdir / op["backup"]
                    if not os.path.isfile(fs_path(bak)):
                        skipped += 1
                        continue
                    if not dry:
                        tmp = _wb_tmp(p)
                        shutil.copy2(fs_path(bak), fs_path(tmp))
                        ensure_writable(p)
                        os.replace(fs_path(tmp), fs_path(p))
                    done += 1
                elif t == "db_rows_inserted":
                    if conn is None:
                        skipped += 1
                        continue
                    table, pks = op["table"], op.get("pks", [])
                    for pkv in pks:
                        row = conn.execute(
                            "SELECT * FROM %s WHERE %s=?" % (table, _pk_col(conn, table) or "rowid"),
                            (pkv,)).fetchone() if _pk_col(conn, table) else None
                        expected = (op.get("row_sha256") or {}).get(str(pkv))
                        if expected and row is not None and _row_sha(row) != expected:
                            _LOG.warning("[SKIP] %s pk=%s 行已变化, 不删除" % (table, pkv))
                            continue
                        if not dry:
                            conn.execute(
                                "DELETE FROM %s WHERE %s=?" % (table, _pk_col(conn, table)),
                                (pkv,))
                    if not dry:
                        pass
                    done += 1
                elif t == "db_row_overwritten":
                    if conn is None:
                        skipped += 1
                        continue
                    table, pk, old = op["table"], op["pk"], op.get("old", {})
                    if not dry and old:
                        sets = ",".join("%s=?" % c for c in old)
                        conn.execute("UPDATE %s SET %s WHERE %s=?" % (table, sets, _pk_col(conn, table) or "rowid"),
                                     list(old.values()) + [pk])
                    done += 1
                elif t == "db_row_renamed":
                    if conn is None:
                        skipped += 1
                        continue
                    table = op["table"]
                    pkc = _pk_col(conn, table)
                    if not pkc:
                        skipped += 1
                        continue
                    row = conn.execute("SELECT * FROM %s WHERE %s=?" % (table, pkc),
                                       (op["new_pk"],)).fetchone()
                    if row is None:
                        skipped += 1
                        continue
                    if op.get("row_sha256") and _row_sha(row) != op["row_sha256"]:
                        _LOG.warning("[SKIP] %s pk=%s 行已变化" % (table, op["new_pk"]))
                        skipped += 1
                        continue
                    if not dry:
                        conn.execute("DELETE FROM %s WHERE %s=?" % (table, pkc),
                                     (op["new_pk"],))
                    done += 1
                elif t == "db_rows_updated":
                    if conn is None:
                        skipped += 1
                        continue
                    table, col, rows = op["table"], op["column"], op.get("rows", [])
                    pkc = _pk_col(conn, table)
                    if not pkc:
                        skipped += 1
                        continue
                    if not dry:
                        for pair in rows:
                            if len(pair) == 2:
                                conn.execute("UPDATE %s SET %s=? WHERE %s=?" % (table, col, pkc),
                                             (pair[1], pair[0]))
                    done += 1
                elif t == "db_rows_deleted":
                    if conn is None:
                        skipped += 1
                        continue
                    table = op["table"]
                    rows = op.get("rows", [])
                    if not dry:
                        cols = set(_table_columns(conn, table))
                        for row in rows:
                            keep = {k: v for k, v in row.items() if k in cols}
                            if not keep:
                                continue
                            pkc = _pk_col(conn, table)
                            existed = conn.execute(
                                "SELECT 1 FROM %s WHERE %s=?" % (table, pkc),
                                (keep.get(pkc),)).fetchone() if pkc and pkc in keep else None
                            if existed:
                                sets = ",".join("%s=?" % c for c in keep)
                                conn.execute("UPDATE %s SET %s WHERE %s=?" % (table, sets, pkc),
                                             list(keep.values()) + [keep.get(pkc)])
                            else:
                                conn.execute(
                                    "INSERT INTO %s (%s) VALUES (%s)"
                                    % (table, ",".join(keep), ",".join(["?"] * len(keep))),
                                    list(keep.values()))
                    done += 1
                elif t == "dir_renamed":
                    frm, to = wb_home / op["frm"], wb_home / op["to"]
                    if not to.is_dir():
                        skipped += 1
                        continue
                    if not dry:
                        if frm.is_dir():
                            merge_leftover = frm.with_name(frm.name + ".wb-conflict-%s" % now_ts())
                            shutil.move(fs_path(to), fs_path(merge_leftover))
                        else:
                            shutil.move(fs_path(to), fs_path(frm))
                    done += 1
                elif t == "dir_created":
                    p = wb_home / op["path"]
                    if not p.is_dir():
                        skipped += 1
                        continue
                    if not dry:
                        try:
                            p.rmdir()
                        except OSError:
                            _LOG.warning("[SKIP] 目录非空, 保留: %s" % p)
                            skipped += 1
                            continue
                    done += 1
                elif t == "file_moved":
                    # 切片/移动可能是文件也可能是整个目录, 两者都要能撤销
                    frm, to = wb_home / op["frm"], wb_home / op["to"]
                    if not to.exists():
                        skipped += 1
                        continue
                    if not dry:
                        _mkdir(frm.parent)
                        if frm.exists():
                            # 源位置在失败前又被写入了新内容, 先让位, 避免覆盖
                            conflict = frm.with_name(
                                frm.name + ".wb-rollback-conflict-" + now_ts())
                            shutil.move(fs_path(frm), fs_path(conflict))
                            _LOG.warning("[回滚] 源位置已占用, 原内容另存为 %s"
                                         % conflict.name)
                        shutil.move(fs_path(to), fs_path(frm))
                    done += 1
                elif t == "dir_merged":
                    if not dry:
                        for mv in op.get("moved", []):
                            to = wb_home / mv
                            frm = wb_home / op["dest"] / Path(mv).name
                            if to.is_file() and not frm.exists():
                                _mkdir(frm.parent)
                                shutil.move(fs_path(to), fs_path(frm))
                        leftover = wb_home / op.get("leftover", "")
                        if leftover.is_dir():
                            rmtree_safe(leftover)
                    done += 1
                else:
                    skipped += 1
            except (sqlite3.Error, OSError, KeyError) as e:
                _LOG.warning("[SKIP] 操作 %s 失败: %s" % (t, e))
                skipped += 1
        if conn is not None:
            conn.close()
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    _LOG.info("回滚完成: 撤销 %d/%d 项, 跳过 %d 项"
              % (done, len(ops), skipped))
    if not dry:
        _LOG.info("建议重启 WorkBuddy 使数据库状态生效")
    return EXIT_OK


def _pk_col(conn: sqlite3.Connection, table: str) -> Optional[str]:
    try:
        for r in conn.execute("PRAGMA table_info(%s)" % table):
            if r[5]:
                return r[1]
    except sqlite3.Error:
        pass
    return None


def _row_sha(row: Iterable) -> str:
    return hashlib.sha256(repr(tuple(row)).encode("utf-8")).hexdigest()


# ============================================================
# [S7] 恢复引擎 — 上下文
# ============================================================

class RestoreContext:
    def __init__(self, wb_home: Path, manifest: Dict, opts: Dict):
        self.wb_home = wb_home
        self.manifest = manifest
        self.opts = opts
        self.conflict: str = opts.get("conflict", "merge")
        self.dry_run: bool = bool(opts.get("dry_run"))
        self.auto_map: bool = bool(opts.get("auto_map"))
        self.modules: List[str] = opts.get("restore_modules") or \
            (manifest.get("options") or {}).get("modules") or list(MODULE_IDS)
        self.ts_suffix: str = "imported-" + now_ts()
        self.source_os: str = (manifest.get("platform") or {}).get("os", "")
        src_prefix = (manifest.get("source") or {}).get("home_prefix", "")
        self.source_home_prefix: str = src_prefix or (
            detect_source_home_prefix(manifest.get("platform") or {}) or "")
        self.target_home_prefix: str = str(Path.home())
        self.source_user_ids: List[str] = (manifest.get("source") or {}).get("user_ids") or []
        self.target_user_id: Optional[str] = opts.get("target_user_id")
        self.imported_session_ids: List[str] = []
        self.session_rename_map: Dict[str, str] = {}
        self.automation_rename_map: Dict[str, str] = {}
        self.needs_remap: bool = False
        self.journal: Optional[RollbackJournal] = None

    def detect(self) -> None:
        """决定是否需要路径重映射 (含同 OS 换用户名 / 换盘符)"""
        if not self.auto_map or not self.source_home_prefix:
            self.needs_remap = False
            return
        src = _norm_prefix(self.source_home_prefix)
        tgt = _norm_prefix(self.target_home_prefix)
        self.needs_remap = src != tgt

    def allowed_tables(self) -> List[str]:
        if self.opts.get("all_tables"):
            return [t for t, _pk, _m in DB_TABLES]
        out = []
        for t, _pk, m in DB_TABLES:
            if m in self.modules:
                out.append(t)
        return out


def _norm_prefix(p: str) -> str:
    p = p.replace("\\", "/").rstrip("/")
    return p


def remap_string(path: str, src_prefix: str, tgt_prefix: str) -> str:
    """把 path 中的源 home 前缀替换为目标 home 前缀 (兼容两种分隔符)"""
    if not path or not src_prefix:
        return path
    norm = path.replace("\\", "/")
    src_norm = _norm_prefix(src_prefix)
    if norm.lower().startswith(src_norm.lower()):
        rel = norm[len(src_norm):].lstrip("/")
        if is_windows():
            rel = rel.replace("/", "\\")
            return tgt_prefix.rstrip("\\") + "\\" + rel
        return tgt_prefix.rstrip("/") + "/" + rel.replace("\\", "/")
    return path


# ============================================================
# [S6/S7] 数据库合并 (列交集 + 三种冲突策略)
# ============================================================

def _col_defaults(conn: sqlite3.Connection, table: str,
                  cols: List[str]) -> Dict[str, object]:
    """目标库独有列的填充值: NOT NULL 无默认 -> 类型零值"""
    out: Dict[str, object] = {}
    try:
        info = {r[1]: r for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return out
    for c in cols:
        r = info.get(c)
        if r is None:
            continue
        _cid, name, ctype, notnull, dflt, _pk = r
        if notnull and dflt is None and not _pk:
            t = (ctype or "").upper()
            if "INT" in t:
                out[c] = 0
            elif "REAL" in t or "FLOA" in t or "DOUB" in t:
                out[c] = 0.0
            elif "BLOB" in t:
                out[c] = b""
            else:
                out[c] = ""
    return out


def merge_databases(src_db: Path, tgt_db: Path, ctx: RestoreContext) -> Dict:
    """把源库 (解包后的快照) 合并进目标库. 返回统计."""
    stats = {"tables": {}, "errors": []}
    src = sqlite3.connect("file:%s?mode=ro" % src_db, uri=True)
    tgt = sqlite3.connect(str(tgt_db))
    tgt.isolation_level = None
    src_tables = _db_table_stats_cached(src)
    allowed = ctx.allowed_tables()
    journal = ctx.journal

    for table, pk, module in DB_TABLES:
        if table not in allowed or table in DB_TABLES_SKIP:
            continue
        if table not in src_tables:
            continue
        src_cols = _table_columns(src, table)
        tgt_cols = _table_columns(tgt, table)
        if not tgt_cols:
            if ctx.opts.get("create_missing_tables"):
                ddl = _table_ddl(src, table)
                if ddl:
                    try:
                        tgt.execute("BEGIN IMMEDIATE")
                        tgt.execute(ddl)
                        tgt.execute("COMMIT")
                        tgt_cols = _table_columns(tgt, table)
                        _LOG.info("    已创建缺失表 %s" % table)
                    except sqlite3.Error as e:
                        tgt.execute("ROLLBACK")
                        stats["errors"].append("建表 %s 失败: %s" % (table, e))
                        continue
            if not tgt_cols:
                _LOG.warning("    [SKIP] 目标库缺少表 %s, 跳过 (建议先启动一次 WorkBuddy 建表)" % table)
                continue

        common = [c for c in src_cols if c in tgt_cols]
        if not common:
            stats["errors"].append("表 %s 无公共列" % table)
            continue
        tgt_only = [c for c in tgt_cols if c not in src_cols]
        defaults = _col_defaults(tgt, table, tgt_only)
        insert_cols = common + tgt_only
        insert_sql = "INSERT OR IGNORE INTO %s (%s) VALUES (%s)" % (
            table, ",".join('"%s"' % c for c in insert_cols),
            ",".join("?" * len(insert_cols)))
        pk_col = pk if pk in common else _pk_col(tgt, table)

        tstat = {"inserted": 0, "skipped": 0, "updated": 0, "renamed": 0,
                 "errors": 0, "src_rows": 0}
        inserted_pks: List[str] = []
        inserted_shas: Dict[str, str] = {}
        try:
            tgt.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            pass
        try:
            for row in src.execute('SELECT %s FROM "%s"' % (
                    ",".join('"%s"' % c for c in common), table)):
                check_interrupt()
                tstat["src_rows"] += 1
                values = dict(zip(common, row))
                pk_val = values.get(pk_col) if pk_col else None
                if pk_val is None:
                    tstat["errors"] += 1
                    continue
                # rename 策略: 子表外键联动
                if table in ("automation_runs", "automation_runtime_state",
                             "automation_delivery_outbox"):
                    ref = values.get("automation_id")
                    if ref in ctx.automation_rename_map:
                        values["automation_id"] = ctx.automation_rename_map[ref]
                # rename 策略: session_usage.session_id 联动
                if table == "session_usage":
                    ref = values.get("session_id")
                    if ref in ctx.session_rename_map:
                        values["session_id"] = ctx.session_rename_map[ref]

                exists = None
                if pk_col:
                    exists = tgt.execute(
                        'SELECT 1 FROM "%s" WHERE "%s"=?' % (table, pk_col),
                        (pk_val,)).fetchone()

                if exists is None:
                    args = [values[c] for c in common] + \
                           [defaults.get(c) for c in tgt_only]
                    try:
                        cur = tgt.execute(insert_sql, args)
                        if cur.rowcount == 1:
                            tstat["inserted"] += 1
                            inserted_pks.append(str(pk_val))
                            # 哈希以实际插入的完整行为准 (含目标独有列默认值)
                            inserted_shas[str(pk_val)] = _row_sha(args)
                            if table == "sessions":
                                ctx.imported_session_ids.append(pk_val)
                        else:
                            tstat["skipped"] += 1
                    except sqlite3.Error as e:
                        tstat["errors"] += 1
                        stats["errors"].append("%s 插入失败 pk=%s: %s" % (table, pk_val, e))
                elif ctx.conflict == "merge":
                    tstat["skipped"] += 1
                elif ctx.conflict == "overwrite":
                    old = tgt.execute(
                        'SELECT %s FROM "%s" WHERE "%s"=?' % (
                            ",".join('"%s"' % c for c in common), table, pk_col),
                        (pk_val,)).fetchone()
                    if old is not None and journal is not None:
                        journal.db_row_overwritten(table, str(pk_val),
                                                   dict(zip(common, old)))
                    sets = ",".join('"%s"=?' % c for c in common if c != pk_col)
                    try:
                        tgt.execute('UPDATE "%s" SET %s WHERE "%s"=?' % (table, sets, pk_col),
                                    [values[c] for c in common if c != pk_col] + [pk_val])
                        tstat["updated"] += 1
                    except sqlite3.Error as e:
                        tstat["errors"] += 1
                elif ctx.conflict == "rename":
                    new_pk = "%s.%s" % (pk_val, ctx.ts_suffix)
                    values[pk_col] = new_pk
                    args = [values[c] for c in common] + \
                           [defaults.get(c) for c in tgt_only]
                    try:
                        cur = tgt.execute(insert_sql, args)
                        if cur.rowcount == 1:
                            tstat["renamed"] += 1
                            if journal is not None:
                                journal.db_row_renamed(table, str(pk_val), str(new_pk),
                                                       _row_sha(args))
                            if table == "sessions":
                                ctx.session_rename_map[str(pk_val)] = str(new_pk)
                            elif table == "automations":
                                ctx.automation_rename_map[str(pk_val)] = str(new_pk)
                        else:
                            tstat["skipped"] += 1
                    except sqlite3.Error as e:
                        tstat["errors"] += 1
            tgt.execute("COMMIT")
        except (sqlite3.Error, InterruptedError_) as e:
            try:
                tgt.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(e, InterruptedError_):
                raise
            stats["errors"].append("表 %s 事务失败: %s" % (table, e))
        if inserted_pks and journal is not None:
            journal.db_rows_inserted(table, inserted_pks, inserted_shas)
        stats["tables"][table] = tstat
        _LOG.info("    %-32s 源%4d -> 插入%4d 跳过%4d 更新%4d 改名%4d 错误%d"
                  % (table, tstat["src_rows"], tstat["inserted"], tstat["skipped"],
                     tstat["updated"], tstat["renamed"], tstat["errors"]))
    src.close()
    tgt.close()
    return stats


# ============================================================
# [S7] 文件恢复 (三种冲突策略)
# ============================================================

def _ctx_rewrite_rel(ctx: RestoreContext, rel: str) -> str:
    """rename 策略下, session 目录名联动改名"""
    if not ctx.session_rename_map:
        return rel
    parts = rel.split("/")
    if len(parts) >= 2 and parts[0] in ("tasks",):
        if parts[1] in ctx.session_rename_map:
            parts[1] = ctx.session_rename_map[parts[1]]
    elif len(parts) >= 3 and parts[0] == "projects":
        if parts[2] in ctx.session_rename_map:
            parts[2] = ctx.session_rename_map[parts[2]]
    return "/".join(parts)


def _file_sha(p: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(fs_path(p), "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _wb_tmp(dst: Path) -> Path:
    """临时文件名 (用于原子替换); 名字保持在 MAX_PATH 友好范围内"""
    n = dst.name
    if len(n) > 60:
        n = n[:60]
    return dst.with_name(n + ".wb-tmp")


def _mkdir(p: Path) -> None:
    """跨平台建目录: 统一走 fs_path, 规避 Windows 长路径/保留名问题"""
    os.makedirs(fs_path(p), exist_ok=True)


def ensure_writable(p: Path) -> None:
    """Windows 只读属性会导致 os.replace/shutil 覆盖失败"""
    if not is_windows():
        return
    try:
        if p.exists() and not os.access(fs_path(p), os.W_OK):
            os.chmod(fs_path(p), 0o600)
    except OSError:
        pass


def restore_file(src: Path, dst: Path, rel: str, ctx: RestoreContext,
                 files_backup_dir: Optional[Path] = None) -> str:
    """恢复单个文件, 返回动作: created/skipped/overwritten/renamed/dry-*"""
    rel_rw = _ctx_rewrite_rel(ctx, rel)
    # Windows: 净化非法字符/保留名/超长路径; 其他平台原样拼接
    dst, changed = safe_join(ctx.wb_home, rel_rw)
    if changed:
        _LOG.warning("文件名已按 Windows 规则净化: %s -> %s"
                     % (surrogate_safe(rel_rw), surrogate_safe(dst.name)))
    if ctx.dry_run:
        if not dst.exists():
            return "dry-created"
        return {"merge": "dry-skipped", "overwrite": "dry-overwritten",
                "rename": "dry-renamed"}[ctx.conflict]
    _mkdir(dst.parent)
    if not dst.exists():
        tmp = _wb_tmp(dst)
        shutil.copy2(fs_path(src), fs_path(tmp))
        os.replace(fs_path(tmp), fs_path(dst))
        if ctx.journal is not None:
            ctx.journal.file_created(rel_rw, _file_sha(dst) or "")
        return "created"
    if ctx.conflict == "merge":
        return "skipped"
    if ctx.conflict == "overwrite":
        ensure_writable(dst)
        if ctx.journal is not None and files_backup_dir is not None:
            bak = files_backup_dir / rel_rw
            _mkdir(bak.parent)
            shutil.copy2(fs_path(dst), fs_path(bak))
            ctx.journal.file_overwritten(rel_rw, str(bak.relative_to(ctx.journal.dir)),
                                         _file_sha(dst) or "")
        tmp = _wb_tmp(dst)
        shutil.copy2(fs_path(src), fs_path(tmp))
        os.replace(fs_path(tmp), fs_path(dst))
        return "overwritten"
    # rename
    dst2 = dst.with_name(dst.name + "." + ctx.ts_suffix)
    shutil.copy2(fs_path(src), fs_path(dst2))
    if ctx.journal is not None:
        ctx.journal.file_created(str(dst2.relative_to(ctx.wb_home)),
                                 _file_sha(dst2) or "")
    return "renamed"


def restore_module_dir(pkg_module_dir: Path, ctx: RestoreContext,
                       files_backup_dir: Optional[Path] = None,
                       counters: Optional[Dict] = None) -> None:
    """把包内 modules/<id>/ 下内容恢复到 wb_home 对应位置"""
    if not pkg_module_dir.is_dir():
        return
    if counters is None:
        counters = {}
    for root, dirs, files in walk_fs(pkg_module_dir):
        check_interrupt()
        rel_root = os.path.relpath(root, str(pkg_module_dir)).replace(os.sep, "/")
        for fn in sorted(files):
            rel = (rel_root + "/" + fn) if rel_root != "." else fn
            src_f = Path(root) / fn
            action = restore_file(src_f, None, rel, ctx, files_backup_dir)
            counters[action] = counters.get(action, 0) + 1


# ============================================================
# [S9] 跨平台路径与账号映射
# ============================================================

def _remap_table_column(conn: sqlite3.Connection, table: str, pk: str,
                        column: str, ctx: RestoreContext,
                        json_array: bool = False) -> int:
    """通用: 把表中路径列从源 home 前缀映射到目标; 返回更新行数"""
    try:
        cols = _table_columns(conn, table)
        if not cols or column not in cols or (pk and pk not in cols):
            return 0
        rows = conn.execute('SELECT "%s", "%s" FROM "%s"' % (pk, column, table)).fetchall()
    except sqlite3.Error:
        return 0
    pairs: List[List[object]] = []
    n = 0
    for pk_val, val in rows:
        if not val:
            continue
        new_val = val
        if json_array:
            try:
                arr = json.loads(val)
                if isinstance(arr, list):
                    arr = [remap_string(x, ctx.source_home_prefix, ctx.target_home_prefix)
                           if isinstance(x, str) else x for x in arr]
                    new_val = json.dumps(arr, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                new_val = val
        else:
            new_val = remap_string(val, ctx.source_home_prefix, ctx.target_home_prefix)
        if new_val != val:
            try:
                conn.execute('UPDATE "%s" SET "%s"=? WHERE "%s"=?' % (table, column, pk),
                             (new_val, pk_val))
                pairs.append([pk_val, val])
                n += 1
            except sqlite3.Error:
                pass
    if pairs and ctx.journal is not None:
        ctx.journal.db_rows_updated(table, column, pairs)
    return n


def apply_db_remapping(conn: sqlite3.Connection, ctx: RestoreContext) -> None:
    """auto-map: sessions.cwd / workspaces.path / trust key / automations.cwds / runs.source_cwd"""
    n = _remap_table_column(conn, "sessions", "id", "cwd", ctx)
    _LOG.info("    sessions.cwd 映射 %d 条" % n)
    n = _remap_table_column(conn, "workspaces", "path", "path", ctx)
    _LOG.info("    workspaces.path 映射 %d 条" % n)
    n = _remap_table_column(conn, "workspace_trust_decisions", "workspace_key",
                            "workspace_key", ctx)
    _LOG.info("    workspace_trust_decisions 映射 %d 条" % n)
    n = _remap_table_column(conn, "automations", "id", "cwds", ctx, json_array=True)
    _LOG.info("    automations.cwds 映射 %d 条" % n)
    n = _remap_table_column(conn, "automation_runs", "thread_id", "source_cwd", ctx)
    _LOG.info("    automation_runs.source_cwd 映射 %d 条" % n)


def _select_target_user_id(conn: sqlite3.Connection) -> Optional[str]:
    try:
        cols = set(_table_columns(conn, "sessions"))
        if not cols:
            return None
        order_cols = [c for c in ("last_activity_at", "updated_at", "created_at")
                      if c in cols]
        if order_cols:
            sql = ("SELECT user_id FROM sessions WHERE user_id IS NOT NULL "
                   "GROUP BY user_id ORDER BY MAX(COALESCE(%s)) DESC LIMIT 1"
                   % ", ".join(order_cols))
        else:
            sql = ("SELECT user_id FROM sessions WHERE user_id IS NOT NULL "
                   "GROUP BY user_id LIMIT 1")
        row = conn.execute(sql).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def merge_user_ids(tgt_conn: sqlite3.Connection, src_db: Path,
                   ctx: RestoreContext) -> None:
    """auto-map: 源账号 user_id -> 目标账号 (仅限备份包内出现的记录, 修复 v2 全表 UPDATE)"""
    target_uid = ctx.target_user_id or _select_target_user_id(tgt_conn)
    if not target_uid:
        _LOG.warning("    无法检测目标账号 user_id (目标库无会话); "
                     "可用 --target-user-id 显式指定, 本次跳过账号合并")
        return
    src = None
    try:
        src = sqlite3.connect("file:%s?mode=ro" % src_db, uri=True)
    except sqlite3.Error:
        return
    try:
        for suid in ctx.source_user_ids:
            if suid == target_uid:
                continue
            pairs_s, pairs_a = [], []
            try:
                sids = [r[0] for r in src.execute(
                    "SELECT id FROM sessions WHERE user_id=?", (suid,))]
            except sqlite3.Error:
                sids = []
            for i in range(0, len(sids), 500):
                chunk = sids[i:i + 500]
                q = ",".join("?" * len(chunk))
                cur = tgt_conn.execute(
                    "UPDATE sessions SET user_id=? WHERE user_id=? AND id IN (%s)" % q,
                    [target_uid, suid] + chunk)
                pairs_s += [[sid, suid] for sid in chunk[:cur.rowcount]]
            try:
                aids = [r[0] for r in src.execute(
                    "SELECT id FROM automations WHERE owner_user_id=?", (suid,))]
            except sqlite3.Error:
                aids = []
            for i in range(0, len(aids), 500):
                chunk = aids[i:i + 500]
                q = ",".join("?" * len(chunk))
                cur = tgt_conn.execute(
                    "UPDATE automations SET owner_user_id=?, owner_status='confirmed' "
                    "WHERE owner_user_id=? AND id IN (%s)" % q,
                    [target_uid, suid] + chunk)
                pairs_a += [[aid, suid] for aid in chunk[:cur.rowcount]]
            if pairs_s and ctx.journal is not None:
                ctx.journal.db_rows_updated("sessions", "user_id", pairs_s)
            if pairs_a and ctx.journal is not None:
                ctx.journal.db_rows_updated("automations", "owner_user_id", pairs_a)
            if pairs_s or pairs_a:
                _LOG.info("    账号合并: %s -> %s (sessions %d, automations %d)"
                          % (suid[:8] + "...", target_uid[:8] + "...",
                             len(pairs_s), len(pairs_a)))
    finally:
        src.close()


def remap_project_dirs(wb_home: Path, src_db: Path, ctx: RestoreContext) -> None:
    """按 cwd 编码重命名 projects/<encoded> 目录; 冲突则合并且不删源 (修复 v2 rmtree)"""
    try:
        src = sqlite3.connect("file:%s?mode=ro" % src_db, uri=True)
        cwds = [r[0] for r in src.execute(
            "SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL")]
        src.close()
    except sqlite3.Error:
        return
    projects_dir = wb_home / "projects"
    for cwd in cwds:
        check_interrupt()
        old_enc = cwd_to_project_dir(cwd)
        new_cwd = remap_string(cwd, ctx.source_home_prefix, ctx.target_home_prefix)
        new_enc = cwd_to_project_dir(new_cwd)
        if not old_enc or old_enc == new_enc:
            continue
        src_dir = projects_dir / old_enc
        if not src_dir.is_dir():
            continue
        tgt_dir = projects_dir / new_enc
        if not tgt_dir.exists():
            if ctx.dry_run:
                _LOG.info("    [DRY] 重命名 projects/%s -> projects/%s" % (old_enc, new_enc))
                continue
            _mkdir(tgt_dir.parent)
            shutil.move(fs_path(src_dir), fs_path(tgt_dir))
            if ctx.journal is not None:
                ctx.journal.dir_renamed("projects/%s" % old_enc, "projects/%s" % new_enc)
            _LOG.info("    projects/%s -> projects/%s" % (old_enc, new_enc))
        else:
            # 合并: 仅移动目标缺失的文件, 源目录改名保留 (绝不删除)
            if ctx.dry_run:
                _LOG.info("    [DRY] 合并 projects/%s -> projects/%s" % (old_enc, new_enc))
                continue
            moved: List[str] = []
            for root, _dirs, files in walk_fs(src_dir):
                for fn in files:
                    s = Path(root) / fn
                    rel = os.path.relpath(str(s), str(src_dir))
                    d = tgt_dir / rel
                    if not d.exists():
                        _mkdir(d.parent)
                        shutil.move(fs_path(s), fs_path(d))
                        moved.append(str(d.relative_to(wb_home)))
            leftover = src_dir.with_name(src_dir.name + ".wb-migrate-orig-%s" % now_ts())
            shutil.move(fs_path(src_dir), fs_path(leftover))
            if ctx.journal is not None:
                ctx.journal.dir_merged("projects/%s" % new_enc, moved,
                                       str(leftover.relative_to(wb_home)))
            _LOG.info("    projects/%s 合并进 %s (%d 文件, 残余保留于 %s)"
                      % (old_enc, new_enc, len(moved), leftover.name))


def remap_shared_json_files(wb_home: Path, ctx: RestoreContext) -> None:
    """cloudstudio-deploy-history 等分享文件中的绝对路径映射 (JSON 感知, 混合分隔符安全)"""
    d = wb_home / "cloudstudio-deploy-history"
    if not d.is_dir() or ctx.dry_run:
        return

    def _walk(obj):
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(v) for v in obj]
        if isinstance(obj, str):
            new = remap_string(obj, ctx.source_home_prefix, ctx.target_home_prefix)
            if new != obj:
                return new
            # 前缀不在开头时做文本级替换 (两种分隔符变体)
            for v in (ctx.source_home_prefix.replace("\\", "/"),
                      ctx.source_home_prefix.replace("/", "\\")):
                if v and v in obj:
                    return obj.replace(v, ctx.target_home_prefix)
            return obj
        return obj

    n = 0
    for jf in d.glob("*.json"):
        try:
            text = read_text_safe(jf)
            obj = json.loads(text)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        new_obj = _walk(obj)
        if new_obj != obj:
            tmp = jf.with_suffix(jf.suffix + ".wb-tmp")
            write_text_lf(tmp, json.dumps(new_obj, ensure_ascii=False, indent=2))
            ensure_writable(jf)
            os.replace(fs_path(tmp), fs_path(jf))
            n += 1
    if n:
        _LOG.info("    分享文件路径映射 %d 个" % n)


def _cwd_matches_current_os(cwd: str) -> bool:
    """cwd 的格式是否与当前系统一致 (Windows 盘符 vs POSIX 绝对/相对路径).
    跨平台恢复时, 源系统的路径不应在本机被创建"""
    if not cwd:
        return False
    n = str(cwd).replace("\\", "/")
    win_style = len(n) >= 2 and n[1] == ":"
    return win_style == is_windows()


def ensure_workspace_dirs(wb_home: Path, db_path: Path, ctx: RestoreContext) -> None:
    """创建缺失的工作区目录 + projects/<encoded> + .workbuddy/memory (修复 v2)"""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        has_deleted = "deleted_at" in set(_table_columns(conn, "sessions"))
        sql = ("SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL"
               + (" AND deleted_at IS NULL" if has_deleted else ""))
        cwds = [r[0] for r in conn.execute(sql)]
        # 注册缺失的 workspaces 记录
        if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='workspaces'").fetchone()[0]:
            for cwd in cwds:
                try:
                    exists = conn.execute("SELECT 1 FROM workspaces WHERE path=?", (cwd,)).fetchone()
                except sqlite3.Error:
                    exists = True
                if not exists:
                    pass  # 只读连接, 注册留给 App
    except sqlite3.Error:
        return
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    projects_dir = wb_home / "projects"
    for cwd in cwds:
        check_interrupt()
        # 跨平台保护: 在 macOS/Linux 上跳过 Windows 盘符路径 (反之亦然),
        # 否则会把 "C:\Users\x\proj" 当成当前系统的相对路径, 在 wb_home 里造出非法目录
        if _cwd_matches_current_os(cwd):
            try:
                wd = Path(cwd)
                created = not wd.exists()
                _mkdir(wd)
                _mkdir(wd / ".workbuddy" / "memory")
                if created:
                    _LOG.info("    已创建工作区目录: %s" % cwd)
            except (OSError, RuntimeError):
                _LOG.warning("    无法创建工作区目录: %s" % cwd)
        enc = cwd_to_project_dir(cwd)
        pdir = projects_dir / enc
        if enc and not pdir.exists():
            if ctx.dry_run:
                continue
            _mkdir(pdir)
            if ctx.journal is not None:
                ctx.journal.dir_created("projects/%s" % enc)


# ============================================================
# [S10] 恢复后校验
# ============================================================

def validate_restore(wb_home: Path, db_path: Optional[Path] = None,
                     modules: Optional[List[str]] = None) -> Dict:
    issues: List[str] = []
    db_path = db_path or (wb_home / DB_MAIN)
    if not db_path.is_file():
        return {"passed": False, "issues": ["数据库文件不存在: %s" % db_path]}
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    modules = modules or list(MODULE_IDS)
    try:
        cols = set(_table_columns(conn, "sessions"))
        if not cols:
            return {"passed": False, "issues": ["sessions 表不存在"]}
        has_deleted = "deleted_at" in cols
        rows = conn.execute(
            "SELECT id, cwd, user_id FROM sessions" +
            (" WHERE deleted_at IS NULL" if has_deleted else "")).fetchall()
        missing_cwd = [r for r in rows if not r[1]]
        if missing_cwd:
            issues.append("%d 条 session 缺少 cwd" % len(missing_cwd))
        gone_ws = [r for r in rows if r[1] and not Path(r[1]).is_dir()]
        if gone_ws:
            issues.append("%d 条 session 的 workspace 目录不存在 (如 %s)"
                          % (len(gone_ws), gone_ws[0][1]))
        if "conversations" in modules:
            projects_dir = wb_home / "projects"
            missing_proj = [r for r in rows
                            if r[1] and not (projects_dir / cwd_to_project_dir(r[1])).is_dir()]
            if missing_proj:
                issues.append("%d 条 session 缺少 project 目录 (如 %s)"
                              % (len(missing_proj), cwd_to_project_dir(missing_proj[0][1])))
        if "tasks" in modules:
            tasks_dir = wb_home / "tasks"
            if tasks_dir.is_dir():
                sids = {r[0] for r in rows}
                orphan = [d.name for d in tasks_dir.iterdir()
                          if d.is_dir() and d.name not in sids]
                if orphan:
                    issues.append("%d 个任务组无对应 session (如 %s)"
                                  % (len(orphan), orphan[0]))
        uids = [r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM sessions WHERE user_id IS NOT NULL")]
        if len(uids) > 1:
            issues.append("存在 %d 个不同 user_id (auto-map 可合并): %s"
                          % (len(uids), ", ".join(u[:8] + "..." for u in uids)))
        n_sessions = len(rows)
        n_skills = len([d for d in (wb_home / "skills").iterdir() if d.is_dir()]) \
            if (wb_home / "skills").is_dir() else 0
        _LOG.info("  校验: sessions=%d, skills=%d, workspaces 目录=%d"
                  % (n_sessions, n_skills, len(rows) - len(gone_ws)))
    except sqlite3.Error as e:
        issues.append("数据库读取失败: %s" % e)
    finally:
        conn.close()
    return {"passed": not issues, "issues": issues}


# ============================================================
# [S5/S7/S9/S10] 恢复编排
# ============================================================

def _legacy_known_entries() -> Set[str]:
    known: Set[str] = set()
    for m in MODULES:
        for d in list(m.get("dirs", [])) + list(m.get("large_dirs", [])):
            known.add(Path(d).parts[0])
        for f in m.get("files", []):
            known.add(f)
    known |= {DB_MAIN, DB_EDGE, "metadata.json", "manifest.json",
              "checksums.sha256", "SOUL.md", "IDENTITY.md", "USER.md",
              "BOOTSTRAP.md", "MEMORY.md"}
    return known


def _legacy_entry_module(name: str) -> Optional[str]:
    for m in MODULES:
        for d in list(m.get("dirs", [])) + list(m.get("large_dirs", [])):
            if Path(d).parts[0] == name:
                return m["id"]
        for f in m.get("files", []):
            if f == name:
                return m["id"]
    if name in (DB_MAIN, DB_EDGE):
        return "__database__"
    return None


def restore_backup(archive: Path, opts: Dict) -> int:
    wb_home: Path = opts["wb_home"]
    if not archive.is_file():
        _LOG.error("备份包不存在: %s" % archive)
        return EXIT_ERROR

    prog = Progress(9, "restore")
    prog.phase("校验备份包完整性")
    vres = verify_archive(archive)
    if not vres["ok"]:
        for e in vres["errors"][:20]:
            _LOG.error("  [FAIL] %s" % e)
        _LOG.error("校验失败, 拒绝恢复 (%d 个错误)" % len(vres["errors"]))
        return EXIT_CHECKSUM
    manifest = vres["manifest"]
    legacy = manifest.get("legacy", False)
    if manifest.get("format_version", 0) not in SUPPORTED_FORMAT_VERSIONS:
        _LOG.error("不支持的包格式版本: %s" % manifest.get("format_version"))
        return EXIT_ERROR

    ctx = RestoreContext(wb_home, manifest, opts)
    ctx.detect()

    # 版本兼容性: 源包 vs 本机
    src_ver = (manifest.get("source") or {}).get("wb_version")
    src_cls = classify_wb_version(src_ver)
    tgt_ver, _tgt_src = detect_wb_version(wb_home)
    tgt_cls = classify_wb_version(tgt_ver)
    _LOG.info("版本: 源包 %s (%s) -> 本机 %s (%s)"
              % (src_cls["label"], src_cls["status"], tgt_cls["label"], tgt_cls["status"]))
    if src_ver and tgt_ver and v_tuple(src_ver) != v_tuple(tgt_ver):
        _LOG.warning("[跨版本] 数据库按「列交集」合并, 差异列保留目标端原值; "
                     "目录结构差异按现有模块自动识别, 未知目录不写入。")
    if tgt_cls["status"] in ("newer", "too-old", "legacy"):
        _LOG.warning("[兼容提示] %s" % tgt_cls["advice"])
    if not ctx.dry_run and check_app_running():
        if is_windows():
            _LOG.error("[占用] 检测到 WorkBuddy 正在运行: Windows 下 %s 被进程锁住无法替换。"
                       "请完全退出 WorkBuddy (含托盘图标) 后重试。" % DB_MAIN)
            return EXIT_PERMISSION
        _LOG.warning("[占用] WorkBuddy 仍在运行, 数据库可能被覆盖写入时被回滚/重建; "
                     "建议退出应用后再恢复。")

    mode_tag = "[DRY-RUN] " if ctx.dry_run else ""
    _LOG.info("%s恢复模式: conflict=%s, modules=%s, auto-map=%s, 格式=v%d%s"
              % (mode_tag, ctx.conflict, ",".join(ctx.modules),
                 "yes" if ctx.auto_map else "no",
                 manifest.get("format_version", 0), " (旧格式)" if legacy else ""))
    red = manifest.get("redaction") or {}
    if red.get("enabled") is True:
        _LOG.info("[安全] 此备份包已脱敏, 凭据不可还原; 连接器在目标端需重新授权")

    # 校验用户选择的模块
    if opts.get("restore_modules"):
        pkg_mods = set((manifest.get("options") or {}).get("modules") or MODULE_IDS)
        for mid in opts["restore_modules"]:
            if mid not in pkg_mods and mid != "other":
                _LOG.warning("备份包中不含模块 %s, 跳过" % mid)
        ctx.modules = [m for m in ctx.modules if m in pkg_mods or m == "other"]

    prog.phase("安全解包到临时目录")
    tmpdir = Path(tempfile.mkdtemp(prefix="wb_restore_"))
    register_cleanup(str(tmpdir))
    safe_extract_tar(archive, tmpdir)

    src_db = tmpdir / "database" / DB_MAIN if not legacy else tmpdir / DB_MAIN
    if not src_db.is_file():
        _LOG.error("备份包中未找到 %s" % DB_MAIN)
        return EXIT_ERROR
    # 旧格式包: manifest 无 user_ids, 从源库补读
    if legacy and not ctx.source_user_ids:
        ctx.source_user_ids = _db_user_ids(src_db)
    edge_pkg = (tmpdir / "database" / DB_EDGE) if not legacy else (tmpdir / DB_EDGE)

    target_db = wb_home / DB_MAIN

    # ---- 影子 DB (dry-run) ----
    shadow_db: Optional[Path] = None
    if ctx.dry_run:
        shadow_db = tmpdir / "shadow.db"
        if target_db.is_file():
            _copy_sqlite_snapshot(target_db, shadow_db)
        else:
            shutil.copy2(fs_path(src_db), fs_path(shadow_db))
    else:
        check_write_permissions([wb_home])
        _mkdir(wb_home)

    # ---- pre-restore 快照 + 回滚日志 ----
    files_backup_dir: Optional[Path] = None
    if not ctx.dry_run and not opts.get("no_backup"):
        prog.phase("创建 pre-restore 快照")
        rid = "restore-%s-%04d" % (now_ts(), random.randint(0, 9999))
        hdir = wb_home / HISTORY_DIRNAME / rid
        _mkdir(hdir)
        ctx.journal = RollbackJournal(hdir, archive, manifest, ctx.conflict, ctx.modules)
        if target_db.is_file():
            _copy_sqlite_snapshot(target_db, hdir / "workbuddy.db.pre")
        files_backup_dir = hdir / "files"
        _LOG.info("回滚快照: %s" % hdir)
    else:
        prog.phase("跳过快照 (--dry-run/--no-backup)")

    db_target_for_merge = shadow_db if shadow_db is not None else target_db

    # ---- 数据库恢复 ----
    prog.phase("数据库合并")
    if ctx.dry_run:
        _LOG.info("  [DRY-RUN] 在影子数据库上模拟合并:")
        stats = merge_databases(src_db, db_target_for_merge, ctx)
    elif not target_db.is_file():
        shutil.copy2(fs_path(src_db), fs_path(target_db))
        if ctx.journal is not None:
            ctx.journal.file_created(DB_MAIN, _file_sha(target_db) or "")
        _LOG.info("  目标数据库不存在, 已整体导入")
        stats = {"tables": {}, "errors": []}
    else:
        stats = merge_databases(src_db, target_db, ctx)
    for e in stats.get("errors", [])[:10]:
        _LOG.warning("  [DB] %s" % e)

    # ---- edge-sync 库: 独立文件复制, 不合并 ----
    if edge_pkg.is_file():
        tgt_edge = wb_home / DB_EDGE
        if ctx.dry_run:
            _LOG.info("  [DRY-RUN] edge-sync 库: %s"
                      % ("跳过(已存在)" if tgt_edge.exists() else "复制"))
        elif not tgt_edge.exists():
            shutil.copy2(fs_path(edge_pkg), fs_path(tgt_edge))
        elif ctx.conflict == "overwrite":
            if ctx.journal is not None and files_backup_dir is not None:
                bak = files_backup_dir / DB_EDGE
                _mkdir(bak.parent)
                shutil.copy2(fs_path(tgt_edge), fs_path(bak))
                ctx.journal.file_overwritten(
                    DB_EDGE, str(bak.relative_to(ctx.journal.dir)), _file_sha(tgt_edge) or "")
            shutil.copy2(fs_path(edge_pkg), fs_path(tgt_edge))
        elif ctx.conflict == "rename":
            renamed_edge = tgt_edge.with_name(tgt_edge.name + "." + ctx.ts_suffix)
            shutil.copy2(fs_path(edge_pkg), fs_path(renamed_edge))
            if ctx.journal is not None:
                ctx.journal.file_created(
                    renamed_edge.relative_to(ctx.wb_home).as_posix(),
                    _file_sha(renamed_edge) or "")
        else:
            _LOG.info("  edge-sync 库已存在, 跳过 (merge)")

    # ---- 文件恢复 ----
    prog.phase("文件恢复")
    counters: Dict[str, int] = {}
    if legacy:
        known = _legacy_known_entries()
        allow_unknown = bool(opts.get("allow_unknown"))
        for child in sorted(tmpdir.iterdir()):
            if child.name in ("database",):
                continue
            if child.name.startswith("._") or child.name == ".DS_Store":
                continue  # macOS AppleDouble 垃圾, 静默跳过
            if child.name not in known and child.name not in (DB_MAIN, DB_EDGE):
                if not allow_unknown:
                    _LOG.warning("  [SKIP] 旧包未知顶层条目 (不在模块白名单): %s"
                                 " (如确需导入加 --allow-unknown)" % child.name)
                    continue
                _LOG.warning("  [WARN] 导入未知条目 (--allow-unknown): %s" % child.name)
            if child.name in ("metadata.json", "manifest.json", "checksums.sha256"):
                continue
            mid = _legacy_entry_module(child.name)
            if mid and mid != "__database__" and mid not in ctx.modules:
                _LOG.info("  [SKIP] 模块 %s 未选中, 跳过 %s" % (mid, child.name))
                continue
            if child.is_dir():
                for root, _dirs, files in walk_fs(child):
                    check_interrupt()
                    rel_root = os.path.relpath(root, str(tmpdir)).replace(os.sep, "/")
                    for fn in sorted(files):
                        rel = rel_root + "/" + fn
                        action = restore_file(Path(root) / fn, None, rel, ctx,
                                              files_backup_dir)
                        counters[action] = counters.get(action, 0) + 1
            elif child.is_file():
                action = restore_file(child, None, child.name, ctx, files_backup_dir)
                counters[action] = counters.get(action, 0) + 1
    else:
        pkg_modules_dir = tmpdir / "modules"
        if pkg_modules_dir.is_dir():
            for mid in ctx.modules + (["other"] if "other" not in ctx.modules else []):
                mdir = pkg_modules_dir / mid
                if not mdir.is_dir():
                    continue
                _LOG.info("  模块 %s ..." % mid)
                restore_module_dir(mdir, ctx, files_backup_dir, counters)
    _LOG.info("  文件动作: " + ", ".join("%s=%d" % kv for kv in sorted(counters.items()))
              if counters else "  无文件需要恢复")

    # ---- auto-map: 路径与账号 (在文件恢复之后, 确保目录已落盘) ----
    prog.phase("路径与账号映射")
    if ctx.auto_map:
        conn = sqlite3.connect(str(db_target_for_merge))
        conn.isolation_level = None
        try:
            if ctx.needs_remap:
                _LOG.info("  源 home %s -> 目标 home %s (跨平台/换用户名)"
                          % (ctx.source_home_prefix, ctx.target_home_prefix))
                apply_db_remapping(conn, ctx)
            else:
                _LOG.info("  home 前缀一致, 无需路径映射")
            merge_user_ids(conn, src_db, ctx)
        finally:
            conn.close()
        remap_project_dirs(wb_home, src_db, ctx)
        remap_shared_json_files(wb_home, ctx)
    else:
        _LOG.info("  未启用 --auto-map (跨平台/跨账号恢复建议启用)")

    # ---- 工作区目录补全 ----
    prog.phase("工作区目录补全")
    ensure_workspace_dirs(wb_home, db_target_for_merge, ctx)

    # ---- 校验 ----
    prog.phase("恢复后校验")
    v = validate_restore(wb_home, db_path=db_target_for_merge, modules=ctx.modules)
    if v["passed"]:
        _LOG.info("[OK] 校验通过")
    else:
        for i in v["issues"]:
            _LOG.warning("  [WARN] %s" % i)

    # ---- 收尾 ----
    prog.phase("完成")
    if ctx.journal is not None:
        jpath = ctx.journal.save()
        _LOG.info("回滚日志: %s" % jpath)
        _LOG.info("如需撤销本次恢复: wb-migrate rollback --latest")
    if red.get("enabled") is True and "connectors" in ctx.modules:
        _LOG.info("[提醒] 连接器凭据已脱敏, 恢复后需在目标端重新授权连接器")
    _LOG.info("恢复完成%s (建议重启 WorkBuddy)" % (" [DRY-RUN 预览]" if ctx.dry_run else ""))
    rmtree_safe(tmpdir)
    return EXIT_OK if v["passed"] else EXIT_OK  # 校验问题以 WARN 呈现, 不阻断


# ============================================================
# [S11] CLI
# ============================================================

def _split_list(s: Optional[str]) -> Optional[List[str]]:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


# ============================================================
# [S11] 环境兼容性自检: compat / selftest
# ============================================================

def _os_version_tuple() -> Tuple:
    if is_windows():
        # Windows: platform.version() 形如 10.0.22631 -> build = 22631
        parts = re.findall(r"\d+", platform.version())
        return tuple(int(x) for x in parts[:3])
    if is_macos():
        return tuple(int(x) for x in platform.mac_ver()[0].split(".")[:3])
    return v_tuple(platform.release())


def _long_paths_enabled() -> Optional[bool]:
    """Windows 是否启用长路径 (>=260) —— 未启用时由 fs_path() 兜底加 \\\\?\\ 前缀"""
    if not is_windows():
        return None
    try:
        import winreg  # type: ignore
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem")
        val, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
        winreg.CloseKey(key)
        return int(val) == 1
    except Exception:
        return None


def _case_sensitive_fs(test_root: Path) -> Optional[bool]:
    try:
        _mkdir(test_root)
        a = test_root / ".wbcsA"
        b = test_root / ".wbcsa"
        write_text_lf(a, "a")
        cs = not b.exists()
        try:
            os.remove(fs_path(a))
        except OSError:
            pass
        return cs
    except OSError:
        return None


def build_compat_report(wb_home: Path) -> Dict:
    info: Dict = {}
    warns: List[str] = []
    errors: List[str] = []

    py_ok = sys.version_info[:2] >= PY_MIN_VERSION
    info["python"] = "%d.%d.%d" % sys.version_info[:3]
    if not py_ok:
        errors.append("Python %s 过低, 需要 >= %d.%d"
                      % (info["python"], PY_MIN_VERSION[0], PY_MIN_VERSION[1]))

    osv = _os_version_tuple()
    info["os"] = "%s %s (%s)" % (platform.system(), platform.release(), platform.machine())
    info["os_version"] = ".".join(str(x) for x in osv)
    if is_windows():
        build = osv[2] if len(osv) >= 3 else 0
        if build and build < WINDOWS_MIN_BUILD:
            errors.append("Windows build %d 过低, 需要 %s (build %d) 以上"
                          % (build, WINDOWS_MIN_NAME, WINDOWS_MIN_BUILD))
        info["long_paths_enabled"] = _long_paths_enabled()
        if info["long_paths_enabled"] is False:
            warns.append("未开启长路径支持 (HKLM\\...\\FileSystem\\LongPathsEnabled=1); "
                         "已由脚本自动加 \\\\?\\ 前缀兜底, 超长路径仍建议迁移到短目录")
    elif is_macos():
        if osv and osv < MACOS_MIN_VERSION:
            warns.append("macOS 版本较低 (< %s), 未在该版本上验证"
                         % ".".join(str(x) for x in MACOS_MIN_VERSION))
    elif is_linux():
        if osv and osv[:2] < LINUX_MIN_KERNEL:
            warns.append("Linux 内核较旧 (< %s)"
                         % ".".join(str(x) for x in LINUX_MIN_KERNEL))
    else:
        warns.append("未验证的操作系统: %s" % platform.system())

    info["filesystem_encoding"] = sys.getfilesystemencoding()
    info["stdout_encoding"] = getattr(sys.stdout, "encoding", "unknown")
    if str(info["filesystem_encoding"]).lower().replace("-", "") not in ("utf8",):
        warns.append("文件系统编码为 %s, 非 UTF-8: 含非 ASCII 的文件名可能丢失"
                     % info["filesystem_encoding"])

    info["case_sensitive_fs"] = fs_case_sensitive()

    info["sqlite"] = sqlite3.sqlite_version
    info["tool_version"] = VERSION
    info["wb_home"] = str(wb_home)
    info["wb_home_exists"] = wb_home.is_dir()
    if not info["wb_home_exists"]:
        warns.append("WorkBuddy 数据目录不存在: %s (新机器首次运行属正常)" % wb_home)

    wb_ver, wb_src = detect_wb_version(wb_home)
    cls = classify_wb_version(wb_ver)
    info["wb_version"] = cls["label"]
    info["wb_version_source"] = wb_src
    info["wb_version_status"] = cls["status"]
    info["wb_version_advice"] = cls["advice"]
    if cls["status"] == "too-old":
        errors.append("WorkBuddy 版本过旧: %s" % cls["advice"])
    elif cls["status"] != "supported":
        warns.append("WorkBuddy %s: %s" % (cls["label"], cls["advice"]))

    info["app_running"] = check_app_running()
    if info["app_running"]:
        warns.append("WorkBuddy 正在运行: 恢复前需完全退出, 否则 Windows 下替换数据库会失败")

    free = disk_free(wb_home if wb_home.is_dir() else Path.cwd())
    info["disk_free"] = human_size(free) if free >= 0 else "unknown"

    return {"ok": not errors, "info": info, "warnings": warns, "errors": errors}


def cmd_compat(opts: Dict) -> int:
    wb_home: Path = opts["wb_home"]
    rep = build_compat_report(wb_home)
    info = rep["info"]
    _LOG.info("== 环境兼容性报告 ==")
    _LOG.info("脚本版本      : %s (包格式 v%d)"
              % (info["tool_version"], BACKUP_FORMAT_VERSION))
    _LOG.info("系统          : %s" % info["os"])
    if os_ver := info["os_version"]:
        _LOG.info("系统版本      : %s" % os_ver)
    _LOG.info("Python        : %s" % info["python"])
    _LOG.info("SQLite        : %s" % info["sqlite"])
    _LOG.info("文件编码      : %s / 控制台 %s"
              % (info["filesystem_encoding"], info["stdout_encoding"]))
    cs_desc = {True: "敏感", False: "不敏感 (同名不同大小写将走 rename 兜底)",
               None: "未知"}[info["case_sensitive_fs"]]
    _LOG.info("文件系统大小写: %s" % cs_desc)
    if info.get("long_paths_enabled") is not None:
        _LOG.info("长路径支持    : %s" % ("已开启" if info["long_paths_enabled"]
                                         else "未开启 (脚本自动兜底)"))
    _LOG.info("数据目录      : %s (%s)"
              % (info["wb_home"], "存在" if info["wb_home_exists"] else "不存在"))
    _LOG.info("磁盘可用      : %s" % info["disk_free"])
    _LOG.info("WorkBuddy     : %s  [%s]  来源: %s"
              % (info["wb_version"], info["wb_version_status"], info["wb_version_source"]))
    _LOG.info("支持范围      : %s ~ %s (实测 %s)"
              % (WB_MIN_VERSION, WB_TARGET_VERSION, "/".join(WB_TESTED_VERSIONS)))
    _LOG.info("应用运行状态  : %s" % ("运行中" if info["app_running"] else "未运行"))
    if rep["warnings"]:
        _LOG.info("")
        _LOG.info("警告 (%d):" % len(rep["warnings"]))
        for w in rep["warnings"]:
            _LOG.info("  [WARN] %s" % w)
    if rep["errors"]:
        _LOG.info("")
        for e in rep["errors"]:
            _LOG.error("  [FAIL] %s" % e)
    _LOG.info("")
    _LOG.info("结论: %s" % ("通过, 可执行备份/恢复" if rep["ok"]
                            else "存在阻断项, 请先处理上述 FAIL"))
    _LOG.info("建议在目标机运行一次: wb-migrate selftest   (端到端一致性自检)")
    return EXIT_OK if rep["ok"] else EXIT_ERROR


# ---------------------------- 端到端自检 ----------------------------

def _st_case(name: str, results: List[Dict], fn) -> None:
    t0 = time.time()
    try:
        detail = fn()
        results.append({"name": name, "ok": True, "detail": detail or "ok",
                        "ms": int((time.time() - t0) * 1000)})
        _LOG.info("  [PASS] %-34s %s" % (name, detail or ""))
    except AssertionError as e:
        results.append({"name": name, "ok": False, "detail": str(e),
                        "ms": int((time.time() - t0) * 1000)})
        _LOG.error("  [FAIL] %-34s %s" % (name, e))
    except Exception as e:                                   # noqa: BLE001
        results.append({"name": name, "ok": False, "detail": "%s: %s" % (type(e).__name__, e),
                        "ms": int((time.time() - t0) * 1000)})
        _LOG.error("  [FAIL] %-34s %s: %s" % (name, type(e).__name__, e))


def _st_make_db(path: Path, new_schema: bool) -> None:
    """new_schema=True 模拟 5.9 (含新增列), False 模拟 5.8 —— 用于验证列交集合并"""
    conn = sqlite3.connect(str(path))

    def _insert(table: str, extra_cols: List[str], vals: List):
        cols = ["id"] + extra_cols
        ddl = ", ".join(["id TEXT PRIMARY KEY"] + ["%s TEXT" % c for c in extra_cols])
        conn.execute("CREATE TABLE IF NOT EXISTS %s (%s)" % (table, ddl))
        conn.execute("INSERT INTO %s (%s) VALUES (%s)"
                     % (table, ",".join(cols), ",".join(["?"] * len(cols))), vals)

    _insert("sessions", ["user_id", "cwd", "title"] +
            (["transport", "group_id"] if new_schema else []),
            ["s1", "uid-src", "/Users/srcuser/code/demo", "会话-中文"] +
            (["local", "g1"] if new_schema else []))
    _insert("sessions", ["user_id", "cwd", "title"] +
            (["transport", "group_id"] if new_schema else []),
            ["s2", "uid-src", "C:\\Users\\olduser\\proj", "win project"] +
            (["local", None] if new_schema else []))
    _insert("automations", ["title", "cwds"] +
            (["reasoning_effort", "workspace_scope"] if new_schema else []),
            ["a1", "每周周报", "[\"/Users/srcuser/code\"]"] +
            (["high", "workspace"] if new_schema else []))
    conn.execute("CREATE TABLE IF NOT EXISTS migration_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO migration_meta VALUES (?, ?)",
                 ("legacy_history_migration", '{"status":"success"}'))
    conn.commit()
    conn.close()


def _st_make_fixture(root: Path) -> Path:
    """构造最小 5.9 风格数据目录"""
    wb = root / "wb_home"
    _mkdir(wb)

    def _w(rel_or_path, text):
        p = rel_or_path if isinstance(rel_or_path, Path) else wb / rel_or_path
        _mkdir(p.parent)
        write_text_lf(p, text)
    # projects 编码目录 (POSIX 风格 cwd + Windows 风格 cwd 各一个)
    _mkdir(wb / "projects" / "Users-srcuser-code-demo")
    _mkdir(wb / "projects" / "c-Users-olduser-proj")
    _w(wb / "projects" / "Users-srcuser-code-demo" / "session.jsonl",
                  '{"id":"s1","title":"会话-中文"}\n')
    # 记忆 / 技能 / 任务 / 配置
    _w(wb / "memory" / "MEMORY.md", "# 记忆\n中文内容\n")
    _w(wb / "skills" / "demo" / "SKILL.md", "---\nname: demo\n---\n")
    _w(wb / "tasks" / "tasks" / "t1.json", '{"id":"t1","title":"任务"}\n')
    _w(wb / "settings.json", '{"theme":"dark","apiKey":"SECRET-PLAIN-123"}\n')
    _w(wb / "mcp.json", json.dumps(
        {"mcpServers": {"svc": {"url": "https://x.example.com/mcp?apikey=PLAIN123",
                                "headers": {"Authorization": "Bearer TOKEN123"},
                                "env": {"TOKEN": "abc"},
                                "command": "npx -y @x/mcp"}}}))
    _w(wb / "startup-seed-version.json", '{"appVersion":"5.9.0"}\n')
    _w(wb / "desktop_conversation_migrated", "2026-08-10T07:18:27.819Z")
    _w(wb / "cloudstudio-deploy-history" / "share.json",
                  json.dumps({"id": "sh1", "shareLink": "https://wb.cn/s/1",
                              "localPath": "/Users/srcuser/code/demo/index.html"}))
    _mkdir(wb / "connectors" / "demo")
    _w(wb / "connectors" / "demo" / ".master.key", "MASTERKEY-RAW")
    _w(wb / "connectors" / "demo" / "config.json", '{"clientSecret":"CS-PLAIN"}')
    _st_make_db(wb / DB_MAIN, new_schema=True)
    return wb


def _tree_snapshot(root: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for dirpath, _dirs, files in walk_fs(root):
        for fn in files:
            p = Path(dirpath) / fn
            try:
                rel = os.path.relpath(str(p), str(root)).replace(os.sep, "/")
            except ValueError:
                continue
            if rel.startswith(".wb-migrate"):
                continue
            h = hashlib.sha256()
            with open(fs_path(p), "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            out[rel] = h.hexdigest()
    return out


def run_selftest(opts: Dict) -> int:
    """跨平台端到端一致性自检 (含 Windows 规则与跨版本 DB 合入)"""
    root = Path(tempfile.mkdtemp(prefix="wb_selftest_"))
    results: List[Dict] = []
    _LOG.info("== wb-migrate selftest ==  临时目录: %s" % root)
    try:
        src = _st_make_fixture(root)
        tgt_root = root / "target"
        tgt = tgt_root / "wb_home"
        _mkdir(tgt)
        _st_make_db(tgt / DB_MAIN, new_schema=False)   # 模拟较低版本的机器
        write_text_lf(tgt / "startup-seed-version.json", '{"appVersion":"5.8.3"}')

        # C1 Python / 标准库
        def c1():
            assert sys.version_info[:2] >= PY_MIN_VERSION, "Python 版本过低"
            return "%d.%d.%d" % sys.version_info[:3]
        _st_case("C1 Python 版本兼容", results, c1)

        # C2 WAL 换行一致性
        def c2():
            p = root / "lf-probe.json"
            write_text_lf(p, json.dumps({"a": 1, "b": "中文"}))
            data = _read_bytes_safe(p)
            assert b"\r\n" not in data, "写入产生了 CRLF, 会破坏校验值"
            assert read_json(p)["b"] == "中文"
            return "LF 恒定, 哈希稳定"
        _st_case("C2 文本换行统一 LF", results, c2)

        # C3 Windows 文件名规则
        def c3():
            cases = [("CON", "_CON"), ("aux.txt", "_aux.txt"), ("bad<>:name", "bad___name"),
                     ("trail.", "trail"), ("normal.md", "normal.md")]
            bad = [(a, _win_name_rule(a)[0], b) for a, b in cases
                   if _win_name_rule(a)[0] != b]
            assert not bad, "净化规则不符: %s" % bad
            return "5 项规则一致"
        _st_case("C3 Windows 文件名净化", results, c3)

        # C4 projects 目录编码规则 (与当前 OS 无关)
        def c4():
            assert cwd_to_project_dir("/Users/li/code/x") == "Users-li-code-x"
            assert cwd_to_project_dir("C:\\Users\\Bob\\proj") == "c-Users-Bob-proj"
            assert cwd_to_project_dir("") == ""
            return "POSIX/Windows 双向"
        _st_case("C4 projects 目录编码", results, c4)

        # C5 版本判定
        def c5():
            assert classify_wb_version("5.9.0")["status"] == "supported"
            assert classify_wb_version("5.6.2")["status"] == "supported"
            assert classify_wb_version("5.4.0")["status"] == "legacy"
            assert classify_wb_version("6.0.0")["status"] == "newer"
            return "supported/legacy/newer 判定"
        _st_case("C5 WorkBuddy 版本判定", results, c5)

        # C6 版本检测
        def c6():
            ver, src_ = detect_wb_version(src)
            assert ver == "5.9.0", "未检出 5.9.0: %s" % ver
            return "来源 %s" % src_
        _st_case("C6 版本检测(数据目录)", results, c6)

        # C7 备份 + 版本写入清单
        arc = {}

        def c7():
            out = create_backup({"wb_home": src,
                                 "modules": resolve_modules(None),
                                 "output": root / "out",
                                 "exclude_patterns": [], "include_large": False,
                                 "include_other": False, "include_self": True,
                                 "redact": True, "level": 1, "dry_run": False,
                                 "resume": None})
            assert out and os.path.isfile(fs_path(out)), "未生成备份包"
            arc["path"] = out
            return "%s" % human_size(os.path.getsize(fs_path(out)))
        _st_case("C7 创建备份包", results, c7)

        # C8 校验
        def c8():
            r = verify_archive(arc["path"])
            assert r["ok"], "校验失败: %s" % r["errors"][:3]
            m = r["manifest"]
            assert (m.get("source") or {}).get("wb_version") == "5.9.0", "清单缺版本"
            return "manifest + sha256 全通过"
        _st_case("C8 包完整性校验", results, c8)

        # C9 脱敏
        def c9():
            names = []
            data = _read_bytes_safe(arc["path"])
            with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r:") as tf:
                for m in tf.getmembers():
                    if m.isfile():
                        names.append(m.name)
                        if m.name.endswith("mcp.json") or m.name.endswith("config.json") \
                                or m.name.endswith("settings.json"):
                            body = tf.extractfile(m).read().decode("utf-8", "replace")
                            for leak in ("PLAIN123", "TOKEN123", "CS-PLAIN", "SECRET-PLAIN-123"):
                                assert leak not in body, "%s 泄露: %s" % (m.name, leak)
                            assert REDACTION_PLACEHOLDER in body, "%s 未脱敏" % m.name
                            assert b"\r\n" not in body.encode("utf-8"), "包内出现 CRLF"
            assert not any(".master.key" in n for n in names), "master.key 未省略"
            assert not any("desktop_conversation_migrated" in n for n in names), \
                "迁移标记被打包"
            return "凭据已脱敏 + 标记文件已排除"
        _st_case("C9 脱敏与排除", results, c9)

        # C10 dry-run 零写入
        def c10():
            before = _tree_snapshot(tgt)
            rc = restore_backup(arc["path"], {
                "wb_home": tgt, "conflict": "overwrite", "restore_modules": None,
                "dry_run": True, "auto_map": True, "target_user_id": None,
                "no_backup": True, "allow_unknown": False, "all_tables": False,
                "create_missing_tables": False})
            after = _tree_snapshot(tgt)
            assert rc == EXIT_OK, "dry-run 返回 %d" % rc
            assert before == after, "dry-run 产生了写入"
            return "零写入"
        _st_case("C10 dry-run 零写入", results, c10)

        # C11 跨 OS/跨版本恢复到目标机
        def c11():
            rc = restore_backup(arc["path"], {
                "wb_home": tgt, "conflict": "merge", "restore_modules": None,
                "dry_run": False, "auto_map": True, "target_user_id": None,
                "no_backup": False, "allow_unknown": False, "all_tables": False,
                "create_missing_tables": False})
            assert rc == EXIT_OK, "恢复返回 %d" % rc
            assert (tgt / "memory" / "MEMORY.md").is_file(), "记忆未恢复"
            assert (tgt / "skills" / "demo" / "SKILL.md").is_file(), "技能未恢复"
            conn = sqlite3.connect(str(tgt / DB_MAIN))
            try:
                rows = conn.execute("SELECT id FROM sessions ORDER BY id").fetchall()
                assert [r[0] for r in rows] == ["s1", "s2"], "会话未合入: %s" % rows
                auto = conn.execute("SELECT COUNT(*) FROM automations").fetchone()[0]
                assert auto >= 1, "定时任务未合入"
            finally:
                conn.close()
            return "文件+DB 均已合入 (列交集)"
        _st_case("C11 跨版本/跨账号恢复", results, c11)

        # C12 三种冲突策略
        def c12():
            outs = {}
            for strat in ("merge", "overwrite", "rename"):
                rc = restore_backup(arc["path"], {
                    "wb_home": tgt, "conflict": strat, "restore_modules": ["tasks"],
                    "dry_run": False, "auto_map": True, "target_user_id": None,
                    "no_backup": True, "allow_unknown": False, "all_tables": False,
                    "create_missing_tables": False})
                assert rc == EXIT_OK, "%s 返回 %d" % (strat, rc)
                outs[strat] = sorted(p.name for p in (tgt / "tasks" / "tasks").iterdir())
            assert len(outs["rename"]) > len(outs["merge"]), "rename 未生成新副本: %s" % outs
            return "merge/overwrite/rename 均可用"
        _st_case("C12 三种冲突策略", results, c12)

        # C13 回滚
        def c13():
            entry = list_rollbacks(tgt)
            if not entry:
                return "无待回滚记录 (跳过判定)"
            before_n = len(_tree_snapshot(tgt))
            rc = cmd_rollback({"wb_home": tgt, "list": False, "latest": True,
                               "id": None, "dry_run": False, "hard": False,
                               "prune": False, "keep": 5})
            assert rc == EXIT_OK, "回滚返回 %d" % rc
            after_n = len(_tree_snapshot(tgt))
            assert after_n <= before_n, "回滚后文件反而增多"
            return "回滚 %s 记录, 文件 %d -> %d" % (len(entry), before_n, after_n)
        _st_case("C13 回滚逆操作", results, c13)

        # C14 破损包拒绝
        def c14():
            raw = _read_bytes_safe(arc["path"])
            cases = []
            # (1) 传输中断: 截断一半
            p1 = root / "bad-truncated.tar.gz"
            p1.write_bytes(raw[:max(512, len(raw) // 2)])
            cases.append(("截断一半", p1))
            # (2) 磁盘/网络损坏: 中间位翻转 (gzip CRC32 / tar 头校验必不通过)
            ba = bytearray(raw)
            pos = max(1, len(ba) // 2)
            ba[pos] ^= 0xFF
            p2 = root / "bad-flip.tar.gz"
            p2.write_bytes(bytes(ba))
            cases.append(("位翻转", p2))
            detail = []
            for tag, pth in cases:
                rc = restore_backup(pth, {
                    "wb_home": tgt, "conflict": "merge", "restore_modules": None,
                    "dry_run": False, "auto_map": False, "target_user_id": None,
                    "no_backup": True, "allow_unknown": False, "all_tables": False,
                    "create_missing_tables": False})
                assert rc in (EXIT_CHECKSUM, EXIT_ERROR), \
                    "%s 破损包未被拒绝: rc=%d" % (tag, rc)
                detail.append("%s=rc%d" % (tag, rc))
            return " ".join(detail)
        _st_case("C14 破损包拒绝", results, c14)

        # C15 平台适配层
        def c15():
            p = tgt / "deep"
            s = fs_path(p)
            if is_windows():
                longp = str(p) + "/" + ("x" * 300)
                assert fs_path(longp).startswith("\\\\?\\"), "长路径未加前缀"
                return "长路径前缀 OK"
            assert s == str(p), "非 Windows 不应改写路径"
            cs = _case_sensitive_fs(tgt / ".probe")
            return "原生路径, 大小写敏感=%s" % cs
        _st_case("C15 平台路径适配", results, c15)

        # ============ 同设备多账号切换 ============
        UA = "aaaa1111-1111-4111-8111-111111111111"
        UB = "bbbb2222-2222-4222-8222-222222222222"
        awb = root / "acc_home"
        _mkdir(awb)

        def _st_make_account_home(wb: Path, uid_a: str, uid_b: str) -> Path:
            def _w(rel, text):
                p = wb / rel
                _mkdir(p.parent)
                write_text_lf(p, text)
            for uid, tag in ((uid_a, "alpha"), (uid_b, "beta")):
                _w("connectors/%s/config.json" % uid, '{"user":"%s","tag":"%s"}' % (uid, tag))
                _w("storage/user-%s/prefs.json" % uid, '{"theme":"%s"}' % tag)
                _w("memory/%s_memory.md" % uid, "# %s memory\n" % tag)
                _w("tasks/%s/todo.json" % uid, '{"items":["%s-1"]}' % tag)
            _w("settings.json", '{"activeUser":"%s","locale":"zh-CN"}' % uid_b)
            _w("SOUL.md", "# SOUL\nactive=%s\n" % uid_b)
            if (wb / DB_MAIN).is_file():
                try:
                    os.remove(fs_path(wb / DB_MAIN))
                except OSError:
                    pass
            conn = sqlite3.connect(str(wb / DB_MAIN))
            try:
                conn.execute("CREATE TABLE sessions "
                             "(id TEXT PRIMARY KEY, user_id TEXT, title TEXT)")
                conn.execute("CREATE TABLE session_usage "
                             "(id TEXT PRIMARY KEY, session_id TEXT, tokens INTEGER)")
                for i, (uid, tag) in enumerate(((uid_a, "alpha"), (uid_b, "beta"))):
                    sid = "%s-s%d" % (tag, i)
                    conn.execute("INSERT INTO sessions VALUES (?,?,?)",
                                 (sid, uid, "%s session" % tag))
                    conn.execute("INSERT INTO session_usage VALUES (?,?,?)",
                                 ("u%d" % i, sid, 100 + i))
                conn.commit()
            finally:
                conn.close()
            return wb

        _st_make_account_home(awb, UA, UB)

        def _acc_active_files(wb: Path, uid: str) -> bool:
            return (wb / "connectors" / uid / "config.json").is_file()

        # C16 账号识别
        def c16():
            accs = discover_accounts(awb)
            uids = {a["user_id"] for a in accs}
            assert UA in uids, "未识别到账号 A: %s" % uids
            assert UB in uids, "未识别到账号 B: %s" % uids
            active = [a["user_id"] for a in accs if a.get("likely_active")]
            assert len(active) == 1, "当前活跃账号不唯一: %s" % active
            return "识别 %d 个账号, 当前活跃=%s" % (len(accs), active[0][:8])
        _st_case("C16 账号识别与清单", results, c16)

        # C17 双向切换与隔离
        def c17():
            rc = cmd_switch({"wb_home": awb, "to": UA, "from": None,
                             "config_policy": "keep", "db_mode": "keep",
                             "dry_run": False, "auto_rollback": True,
                             "report_file": None})
            assert rc == EXIT_OK, "B->A 返回 %d" % rc
            assert _acc_active_files(awb, UA), "A 数据未就位"
            assert not _acc_active_files(awb, UB), "B 数据未隔离"
            slot_b = account_dir(awb, UB) / ACCOUNT_DATA_DIRNAME
            assert (slot_b / "connectors" / UB / "config.json").is_file(), "B 切片缺失"
            rc2 = cmd_switch({"wb_home": awb, "to": UB, "from": None,
                              "config_policy": "keep", "db_mode": "keep",
                              "dry_run": False, "auto_rollback": True,
                              "report_file": None})
            assert rc2 == EXIT_OK, "A->B 返回 %d" % rc2
            assert _acc_active_files(awb, UB), "B 数据未还原"
            assert not _acc_active_files(awb, UA), "A 数据未隔离"
            return "双向切换 OK, 隔离区=%s" % accounts_root(awb).name
        _st_case("C17 双向切换与隔离", results, c17)

        # C18 冲突策略 + dry-run 零写入
        def c18():
            before = _tree_snapshot(awb)
            rc = cmd_switch({"wb_home": awb, "to": UA, "from": None,
                             "config_policy": "overwrite", "db_mode": "stash",
                             "dry_run": True, "auto_rollback": True,
                             "report_file": None})
            assert rc == EXIT_OK, "dry-run 返回 %d" % rc
            assert before == _tree_snapshot(awb), "dry-run 产生了写入"
            # overwrite: 目标账号的共享配置应覆盖当前值
            _st_make_account_home(awb, UA, UB)
            for d in accounts_root(awb).iterdir():
                rmtree_safe(d)
            cmd_switch({"wb_home": awb, "to": UA, "from": None,
                        "config_policy": "keep", "db_mode": "stash",
                        "dry_run": False, "auto_rollback": True, "report_file": None})
            write_text_lf(awb / "settings.json",
                          '{"activeUser":"%s","locale":"en-US"}' % UA)
            cmd_switch({"wb_home": awb, "to": UB, "from": None,
                        "config_policy": "overwrite", "db_mode": "stash",
                        "dry_run": False, "auto_rollback": True, "report_file": None})
            cur = read_json(awb / "settings.json")
            assert cur.get("locale") == "zh-CN", "overwrite 未生效: %s" % cur
            # stash 模式: 切走后该账号的会话行应从主库移除
            conn = sqlite3.connect(str(awb / DB_MAIN))
            try:
                left = dict(conn.execute(
                    "SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id").fetchall())
            finally:
                conn.close()
            assert UA not in left, "stash 模式未隔离 A 的会话: %s" % left
            assert left.get(UB) == 1, "stash 模式未还原 B 的会话: %s" % left
            return "overwrite 生效, stash 隔离=%s" % list(left.values())
        _st_case("C18 冲突策略与数据库模式", results, c18)

    finally:
        try:
            rmtree_safe(root)
        except OSError:
            pass

    failed = [r for r in results if not r["ok"]]
    _LOG.info("")
    _LOG.info("== 自检结果: %d/%d 通过 ==" % (len(results) - len(failed), len(results)))
    if failed:
        for r in failed:
            _LOG.error("  FAIL %s -> %s" % (r["name"], r["detail"]))
        return EXIT_ERROR
    return EXIT_OK


# ============================================================
# [S12] 同设备多账号切换 (account slot / switch)
#  与跨设备 restore 的区别:
#    restore : 备份包 -> 本机, 合并/覆盖/改名, 需要 --auto-map 做路径与账号映射
#    switch  : 本机 <-> 本机隔离区, 非活跃账号的数据移入
#              ~/.workbuddy/.wb-migrate-accounts/<user_id>/data,
#              活跃位置只保留当前账号数据; 同账号可反复双向切换
# ============================================================

def accounts_root(wb_home: Path) -> Path:
    return wb_home / ACCOUNTS_DIRNAME


def account_dir(wb_home: Path, user_id: str) -> Path:
    p, _c = safe_join(accounts_root(wb_home), _wsafe_component(user_id))
    return p


def _wsafe_component(name: str) -> str:
    """账号 id 作为目录名: 去除分隔符等非法字符 (UUID 天然安全, 这里做兜底)"""
    cleaned, _ch = _win_name_rule(name)
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    return cleaned or "_unknown"


def _uid_from_name(name: str) -> Optional[str]:
    """宽松提取: 参数规范化用 (--to 允许粘贴含前后缀的字符串)"""
    m = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        str(name))
    return m.group(0).lower() if m else None


_UUID_STRICT_RE = re.compile(
    r"^(?:user-)?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{12})(?:[_.-].*)?$")


def _strict_uid_from_name(name: str) -> Optional[str]:
    """严格账号命名识别: <uid> / user-<uid>-xxx / <uid>_memory.md / <uid>.json / <uid>.bak.
    资源类 UUID (artifact-index/<uid>.json 等) 不在扫描目录内, 因此不会被误当作账号"""
    m = _UUID_STRICT_RE.match(str(name))
    return m.group(1).lower() if m else None


# 强信号: 能独立确立"这是一个账号"的来源
#   (刻意排除 artifact-index / file-history / tasks / todos 等资源目录:
#    那里的 UUID 是资源/任务 id, 不是账号)
ACCOUNT_STRONG_DIRS = ("connectors", "storage", "memory", "memery",
                       "plugin-account-state")
# 弱信号: 仅在账号已被强信号确立后, 才把对应目录并入该账号专属数据
ACCOUNT_WEAK_DIRS = ("tasks", "todos", "plans", "experts", "local_storage")
# 账号实体自身的通配形态 (如 assistant 的 user-<uid>-<nanoid> 目录)
ACCOUNT_SCAN_GLOBS = ("user-*", "storage/user-*")


def _iter_account_entries(wb_home: Path) -> List[Tuple[str, str, str, bool]]:
    """扫描账号实体 -> [(uid, rel_posix, source, is_dir)]"""
    out: List[Tuple[str, str, str, bool]] = []
    seen: Set[str] = set()

    def _rel(p: Path) -> str:
        try:
            return os.path.relpath(str(p), str(wb_home)).replace(os.sep, "/")
        except ValueError:
            return ""

    # 1) 已知顶层目录下的账号实体
    for d in ACCOUNT_STRONG_DIRS:
        base = wb_home / d
        if not base.is_dir():
            continue
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            if base.name == "connectors" and not child.is_dir():
                continue            # connectors 下只认账号目录, 不认 json
            uid = _strict_uid_from_name(child.name)
            if not uid:
                continue
            rel = _rel(child)
            if rel and rel not in seen:
                seen.add(rel)
                out.append((uid, rel, "dir:%s" % rel, child.is_dir()))
    # 2) 通配形态的账号目录
    for pat in ACCOUNT_SCAN_GLOBS:
        try:
            matches = sorted(wb_home.glob(pat))
        except (OSError, ValueError):
            continue
        for child in matches:
            uid = _strict_uid_from_name(child.name)
            if not uid:
                continue
            rel = _rel(child)
            if rel and rel not in seen:
                seen.add(rel)
                out.append((uid, rel, "dir:%s" % rel, child.is_dir()))
    # 3) 根级遗留账号目录 <uid>/
    if wb_home.is_dir():
        try:
            children = sorted(wb_home.iterdir())
        except OSError:
            children = []
        for child in children:
            if not child.is_dir():
                continue
            if _strict_uid_from_name(child.name) and child.name.count("-") == 4:
                rel = _rel(child)
                if rel and rel not in seen:
                    seen.add(rel)
                    out.append((child.name.lower(), rel, "dir:%s/" % rel, True))
    return out


def _exclusive_paths_for_uid(wb_home: Path, uid: str) -> List[str]:
    """该账号的『专属数据』路径 (相对 wb_home 的 POSIX 路径).
    强信号确定的 + 弱信号中同 uid 的任务/计划/专家数据"""
    rels: List[str] = []
    for _u, rel, _src, _isdir in _iter_account_entries(wb_home):
        if _u == uid and rel not in rels:
            rels.append(rel)
    for d in ACCOUNT_WEAK_DIRS:
        base = wb_home / d
        if not base.is_dir():
            continue
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            if d in ("local_storage", "experts"):
                continue        # 这些目录下的 UUID 是资源 id, 不做归属
            u = _strict_uid_from_name(child.name)
            if u != uid:
                continue
            rel = "%s/%s" % (d, child.name)
            if rel not in rels:
                rels.append(rel)
    return sorted(rels)


def shared_config_files() -> List[str]:
    """跨账号共享的全局配置文件 (切换时按冲突策略处理)"""
    out: List[str] = []
    for mid in ("config", "assistant", "connectors"):
        m = module_by_id(mid)
        if not m:
            continue
        for f in m.get("files", []):
            if f not in out:
                out.append(f)
    return out


def _dir_stats(root: Path, rels: List[str]) -> Tuple[int, int]:
    files = 0
    total = 0
    for rel in rels:
        p, _c = safe_join(root, rel)
        if p.is_file():
            try:
                total += os.path.getsize(fs_path(p))
                files += 1
            except OSError:
                pass
            continue
        for dirpath, _dirs, fns in walk_fs(p):
            for fn in fns:
                try:
                    total += os.path.getsize(fs_path(Path(dirpath) / fn))
                    files += 1
                except OSError:
                    pass
    return files, total


def _db_sessions_by_user(db_path: Path) -> Dict[str, Dict]:
    """按账号统计会话数与最近活跃时间"""
    out: Dict[str, Dict] = {}
    if not os.path.isfile(fs_path(db_path)):
        return out
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except sqlite3.Error:
        return out
    try:
        cols = set(_table_columns(conn, "sessions"))
        if "user_id" not in cols:
            return out
        ts_col = None
        for cand in ("updated_at", "last_message_at", "last_active_at", "created_at"):
            if cand in cols:
                ts_col = cand
                break
        if ts_col:
            rows = conn.execute(
                "SELECT user_id, COUNT(*), MAX(%s) FROM sessions "
                "WHERE user_id IS NOT NULL GROUP BY user_id" % ts_col).fetchall()
        else:
            rows = conn.execute(
                "SELECT user_id, COUNT(*), NULL FROM sessions "
                "WHERE user_id IS NOT NULL GROUP BY user_id").fetchall()
        for uid, cnt, ts in rows:
            out[str(uid)] = {"sessions": int(cnt or 0), "last_active": ts}
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def _account_last_touch_ms(wb_home: Path, rels: List[str]) -> Optional[int]:
    """账号专属数据的最近修改时间 (毫秒), 用于判断哪个账号最后被使用"""
    newest: Optional[int] = None
    for rel in rels:
        p, _c = safe_join(wb_home, rel)
        try:
            if p.is_file():
                m = int(os.path.getmtime(fs_path(p)) * 1000)
                newest = m if newest is None else max(newest, m)
                continue
            for dirpath, _dirs, fns in walk_fs(p):
                for fn in fns:
                    try:
                        m = int(os.path.getmtime(fs_path(Path(dirpath) / fn)) * 1000)
                    except OSError:
                        continue
                    newest = m if newest is None else max(newest, m)
        except OSError:
            continue
    return newest


def discover_accounts(wb_home: Path) -> List[Dict]:
    """聚合多源识别本机存在的账号"""
    accs: Dict[str, Dict] = {}
    db_main = wb_home / DB_MAIN

    def _ensure(uid: str) -> Dict:
        if uid not in accs:
            accs[uid] = {"user_id": uid, "sources": [], "sessions": 0,
                         "last_active": None, "exclusive": [], "files": 0, "bytes": 0,
                         "sliced": False, "recorded_active": False,
                         "last_touch": None, "state": None}
        return accs[uid]

    for uid, info in _db_sessions_by_user(db_main).items():
        a = _ensure(uid)
        a["sources"].append("db:sessions")
        a["sessions"] = info.get("sessions", 0)
        a["last_active"] = info.get("last_active")

    # 目录线索 (严格命名, 排除资源类 UUID)
    for uid, rel, source, _isdir in _iter_account_entries(wb_home):
        a = _ensure(uid)
        a["sources"].append(source)

    # 隔离区线索: 已切走的账号在活动位置可能再无痕迹, 必须仍可切回
    root = accounts_root(wb_home)
    if root.is_dir():
        try:
            entries = sorted(os.listdir(fs_path(root)))
        except OSError:
            entries = []
        for name in entries:
            if name.startswith("."):
                continue
            d = root / name
            if not d.is_dir():
                continue
            meta = read_json(d / ACCOUNT_META_FILE)
            uid = meta.get("user_id") if isinstance(meta, dict) else None
            if not uid:
                uid = _strict_uid_from_name(name)
            if not uid:
                continue
            a = _ensure(uid)
            a["sources"].append("slot:%s" % ((meta or {}).get("state") or "unknown"))

    # 补全专属数据清单与体量
    for uid, a in accs.items():
        a["exclusive"] = _exclusive_paths_for_uid(wb_home, uid)
        a["files"], a["bytes"] = _dir_stats(wb_home, a["exclusive"])
        slot = account_dir(wb_home, uid)
        a["sliced"] = (slot / ACCOUNT_DATA_DIRNAME).is_dir()
        a["last_touch"] = _account_last_touch_ms(wb_home, a["exclusive"])
        meta = read_json(slot / ACCOUNT_META_FILE)
        if isinstance(meta, dict):
            a["state"] = meta.get("state")
            a["recorded_active"] = (meta.get("state") == "active")
            a["last_switch"] = meta.get("updated_at")
    # 判定当前活跃账号: 切换记录 > 综合活跃时间
    for a in accs.values():
        cands = [x for x in (a.get("last_active"), a.get("last_touch")) if x]
        a["active_score"] = max(cands) if cands else 0
    items = list(accs.values())
    items.sort(key=lambda x: (int(x.get("active_score") or 0), x["user_id"]), reverse=True)
    for i, a in enumerate(items):
        a["likely_active"] = (i == 0)
    recorded = [a for a in items if a.get("recorded_active")]
    if len(recorded) == 1:
        for a in items:
            a["likely_active"] = (a is recorded[0])
    return items


def _latest_active_uid(accounts: List[Dict]) -> Optional[str]:
    for a in accounts:
        if a.get("likely_active"):
            return a["user_id"]
    return accounts[0]["user_id"] if accounts else None


def cmd_accounts(opts: Dict) -> int:
    wb_home: Path = opts["wb_home"]
    if not wb_home.is_dir():
        _LOG.error("WorkBuddy 数据目录不存在: %s" % wb_home)
        return EXIT_ERROR
    accounts = discover_accounts(wb_home)
    if not accounts:
        _LOG.warning("未识别到任何账号 (数据库中没有 session.user_id, 也无账号目录)")
        return EXIT_OK
    if opts.get("json"):
        print(json.dumps(accounts, ensure_ascii=False, indent=2))
        return EXIT_OK
    _LOG.info("== 本机账号清单 (%d) ==" % len(accounts))
    _LOG.info("隔离区: %s" % accounts_root(wb_home))
    for a in accounts:
        tag = "[当前活跃]" if a.get("likely_active") else "          "
        la = a.get("last_active")
        _LOG.info("")
        _LOG.info("账号 %s %s" % (a["user_id"], tag))
        _LOG.info("  会话 %d 条, 最近活跃: %s"
                  % (a["sessions"], str(la) if la else "未知"))
        _LOG.info("  专属数据: %d 文件 / %s%s"
                  % (a["files"], human_size(a["bytes"]),
                     "  (已切片)" if a["sliced"] else "  (未切片)"))
        if a["exclusive"]:
            _LOG.info("  账号路径: %s" % ", ".join(a["exclusive"][:6])
                      + (" ..." if len(a["exclusive"]) > 6 else ""))
        _LOG.info("  识别来源: %s" % ", ".join(sorted(set(a["sources"]))[:5]))
    _LOG.info("")
    _LOG.info("切换命令: wb-migrate switch --to <USER_ID>  [--config-policy keep|overwrite|merge]"
              " [--db-mode keep|stash|merge] [--dry-run]")
    return EXIT_OK


# ---------------------------- 切换执行 ----------------------------

def _copy_rels(wb_home: Path, rels: List[str], dest_root: Path,
               stats: Dict[str, int]) -> None:
    """把 rels 复制到 dest_root (保持相对结构)"""
    for rel in rels:
        src, _c = safe_join(wb_home, rel)
        if not src.exists():
            continue
        dst, _c2 = safe_join(dest_root, rel)
        try:
            if src.is_file():
                _mkdir(dst.parent)
                shutil.copy2(fs_path(src), fs_path(dst))
                stats["files"] = stats.get("files", 0) + 1
            else:
                _mkdir(dst.parent)
                for dirpath, _dirs, fns in walk_fs(src):
                    for fn in fns:
                        sp = Path(dirpath) / fn
                        try:
                            rel_f = os.path.relpath(str(sp), str(src)).replace(os.sep, "/")
                        except ValueError:
                            continue
                        dp, _c3 = safe_join(dst, rel_f)
                        _mkdir(dp.parent)
                        shutil.copy2(fs_path(sp), fs_path(dp))
                        stats["files"] = stats.get("files", 0) + 1
        except OSError as e:
            stats.setdefault("errors", []).append({"path": rel, "reason": str(e)})


def _dir_stats_touch(wb_home: Path, rels: List[str], stats: Dict[str, int]) -> None:
    """dry-run 用: 仅统计会涉及多少文件, 不产生任何写入"""
    files, _size = _dir_stats(wb_home, [r for r in rels if (wb_home / r).exists()]
                              if True else rels)
    stats["files"] = stats.get("files", 0) + files


def _move_to_slot(wb_home: Path, rels: List[str], slot_data: Path,
                  journal: Optional[RollbackJournal], dry: bool,
                  counts: Dict[str, int], errors: List[Dict]) -> None:
    """把当前账号专属数据移入隔离区 (可回滚: journal.file_moved)"""
    for rel in rels:
        src, _c = safe_join(wb_home, rel)
        if not src.exists():
            counts["skipped"] = counts.get("skipped", 0) + 1
            continue
        dst, _c2 = safe_join(slot_data, rel)
        try:
            slot_rel = os.path.relpath(str(dst), str(wb_home)).replace(os.sep, "/")
            if not dry:
                _mkdir(dst.parent)
                stale = None
                if dst.exists():
                    # 隔离区已有同名: 属于历史陈旧副本 (当前活动位置的数据才是权威),
                    # 先临时改名, 移动成功后再删除, 失败则原样还原
                    stale = dst.with_name(dst.name + ".wb-stale-" + now_ts())
                    shutil.move(fs_path(dst), fs_path(stale))
                try:
                    shutil.move(fs_path(src), fs_path(dst))
                except OSError:
                    if stale is not None and stale.exists():
                        shutil.move(fs_path(stale), fs_path(dst))
                    raise
                if stale is not None and stale.exists():
                    if stale.is_dir():
                        rmtree_safe(stale)
                    else:
                        try:
                            os.remove(fs_path(stale))
                        except OSError:
                            pass
                    counts["replaced"] = counts.get("replaced", 0) + 1
                if journal is not None:
                    journal.file_moved(frm=rel, to=slot_rel)
            counts["moved"] = counts.get("moved", 0) + 1
        except OSError as e:
            counts["failed"] = counts.get("failed", 0) + 1
            errors.append({"phase": "stash", "path": rel, "reason": str(e)})
            _LOG.warning("    [失败] %s (%s)" % (rel, e))


def _shared_policy_apply(wb_home: Path, policy: str, current_uid: str,
                         target_uid: str, journal: Optional[RollbackJournal],
                         files_backup_dir: Optional[Path], dry: bool,
                         counts: Dict[str, int], errors: List[Dict]) -> None:
    """同名共享配置的冲突处理: keep / overwrite / merge"""
    cur_slot = account_dir(wb_home, current_uid)
    tgt_slot = account_dir(wb_home, target_uid)
    cur_shared = cur_slot / ACCOUNT_SHARED_DIRNAME
    tgt_shared = tgt_slot / ACCOUNT_SHARED_DIRNAME
    if not tgt_shared.is_dir():
        n = len(shared_config_files())
        counts["shared_skipped"] = counts.get("shared_skipped", 0) + n
        _LOG.info("    目标账号无共享配置快照, 当前 %d 项全局配置原样保留" % n)
        return
    for rel in shared_config_files():
        check_interrupt()
        src = tgt_shared / rel
        if not src.is_file():
            continue
        dst, _c = safe_join(wb_home, rel)
        if not dst.is_file():
            if not dry:
                try:
                    _mkdir(dst.parent)
                    shutil.copy2(fs_path(src), fs_path(dst))
                    if journal is not None:
                        journal.file_created(rel, _file_sha(dst) or "")
                except OSError as e:
                    errors.append({"phase": "shared", "path": rel, "reason": str(e)})
                    counts["failed"] = counts.get("failed", 0) + 1
                    continue
            counts["created"] = counts.get("created", 0) + 1
            continue
        try:
            same = (_file_sha(src) == _file_sha(dst))
        except OSError:
            same = False
        if same:
            counts["shared_skipped"] = counts.get("shared_skipped", 0) + 1
            continue
        if policy == "keep":
            counts["shared_skipped"] = counts.get("shared_skipped", 0) + 1
            continue
        if policy == "overwrite":
            if not dry:
                try:
                    if journal is not None and files_backup_dir is not None:
                        bak = files_backup_dir / rel
                        _mkdir(bak.parent)
                        shutil.copy2(fs_path(dst), fs_path(bak))
                        journal.file_overwritten(
                            rel, str(bak.relative_to(journal.dir)), _file_sha(dst) or "")
                    ensure_writable(dst)
                    tmp = _wb_tmp(dst)
                    shutil.copy2(fs_path(src), fs_path(tmp))
                    os.replace(fs_path(tmp), fs_path(dst))
                except OSError as e:
                    errors.append({"phase": "shared", "path": rel, "reason": str(e)})
                    counts["failed"] = counts.get("failed", 0) + 1
                    continue
            counts["overwritten"] = counts.get("overwritten", 0) + 1
            continue
        # merge: JSON 浅合并 (目标账号的值覆盖当前值, 当前独有键保留); 非 JSON 走 rename 副本
        if not dry:
            try:
                cur_obj = read_json(dst)
                tgt_obj = read_json(src)
                if isinstance(cur_obj, dict) and isinstance(tgt_obj, dict):
                    merged = dict(cur_obj)
                    merged.update(tgt_obj)
                    if journal is not None and files_backup_dir is not None:
                        bak = files_backup_dir / rel
                        _mkdir(bak.parent)
                        shutil.copy2(fs_path(dst), fs_path(bak))
                        journal.file_overwritten(
                            rel, str(bak.relative_to(journal.dir)), _file_sha(dst) or "")
                    write_json(dst, merged)
                    counts["merged"] = counts.get("merged", 0) + 1
                else:
                    bak = files_backup_dir / rel if files_backup_dir else None
                    bak_name = rel + "." + ("imported-" + now_ts())
                    if dry is False and bak is not None:
                        (files_backup_dir / _mkdir(bak_name).parent)
                        shutil.copy2(fs_path(src), fs_path(files_backup_dir / bak_name))
                        if journal is not None:
                            journal.file_created(bak_name, _file_sha(files_backup_dir / bak_name) or "")
                    counts["renamed"] = counts.get("renamed", 0) + 1
            except (OSError, ValueError) as e:
                errors.append({"phase": "shared", "path": rel, "reason": str(e)})
                counts["failed"] = counts.get("failed", 0) + 1
        else:
            if rel.endswith(".json"):
                counts["merged"] = counts.get("merged", 0) + 1
            else:
                counts["renamed"] = counts.get("renamed", 0) + 1


_ACCOUNT_TABLE_USER_COLS = ("user_id", "owner_user_id", "account_id", "created_by")


def _account_db_rows(conn: sqlite3.Connection, table: str, uid: str,
                     session_ids: Optional[Set[str]]) -> List[Dict]:
    """取该账号在 table 中的行 (按 user_id 列, 或 session/task 外键关联)"""
    cols = _table_columns(conn, table)
    if not cols:
        return []
    try:
        if "user_id" in cols:
            cur = conn.execute("SELECT * FROM %s WHERE user_id=?" % table, (uid,))
        elif table == "session_usage" and session_ids is not None and "session_id" in cols:
            if not session_ids:
                return []
            qs = ",".join(["?"] * len(session_ids))
            cur = conn.execute("SELECT * FROM session_usage WHERE session_id IN (%s)" % qs,
                               list(session_ids))
        elif "owner_user_id" in cols:
            cur = conn.execute("SELECT * FROM %s WHERE owner_user_id=?" % table, (uid,))
        else:
            return []
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows
    except sqlite3.Error:
        return []


def _table_has_rows_for(db_path: Path, uid: str) -> Dict[str, List[Dict]]:
    """返回 {table: rows}"""
    if not os.path.isfile(fs_path(db_path)):
        return {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except sqlite3.Error:
        return {}
    out: Dict[str, List[Dict]] = {}
    session_ids: Set[str] = set()
    try:
        cols = set(_table_columns(conn, "sessions"))
        if "user_id" in cols and "id" in cols:
            session_ids = {r[0] for r in conn.execute(
                "SELECT id FROM sessions WHERE user_id=?", (uid,))}
        for table, _pk, _mod in DB_TABLES:
            if table in DB_TABLES_SKIP:
                continue
            rows = _account_db_rows(conn, table, uid, session_ids)
            if rows:
                out[table] = rows
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def _write_rows_json(slot_dir: Path, rows: Dict[str, List[Dict]]) -> Optional[Path]:
    try:
        _mkdir(slot_dir)
        p = slot_dir / ACCOUNT_ROWS_FILE
        write_json(p, {"tables": rows})
        return p
    except OSError:
        return None


def _read_rows_json(slot_dir: Path) -> Dict[str, List[Dict]]:
    j = read_json(slot_dir / ACCOUNT_ROWS_FILE)
    if isinstance(j, dict):
        t = j.get("tables")
        if isinstance(t, dict):
            return t
    return {}


def _db_stash_uid(db_path: Path, uid: str, journal: Optional[RollbackJournal],
                  dry: bool, counts: Dict[str, int], errors: List[Dict]) -> Dict[str, List[Dict]]:
    """把该账号的行抽出到内存并(非 dry)从主库删除; 返回抽出的数据"""
    rows = _table_has_rows_for(db_path, uid)
    if not rows:
        return {}
    if dry:
        counts["rows_stashed"] = counts.get("rows_stashed", 0) + sum(len(v) for v in rows.values())
        return rows
    try:
        conn = sqlite3.connect(str(db_path))
        conn.isolation_level = None
    except sqlite3.Error as e:
        errors.append({"phase": "db-stash", "path": DB_MAIN, "reason": str(e)})
        return rows
    try:
        conn.execute("BEGIN")
        for table, rws in rows.items():
            cols = set(_table_columns(conn, table))
            for row in rws:
                pkc = _pk_col(conn, table)
                if not pkc or pkc not in cols:
                    continue
                pk_val = row.get(pkc)
                try:
                    cur_row = conn.execute(
                        "SELECT * FROM %s WHERE %s=?" % (table, pkc), (pk_val,)).fetchone()
                    if cur_row is not None:
                        conn.execute("DELETE FROM %s WHERE %s=?" % (table, pkc), (pk_val,))
                except sqlite3.Error as e:
                    errors.append({"phase": "db-stash", "path": "%s:%s" % (table, pk_val),
                                   "reason": str(e)})
            if journal is not None:
                journal.db_rows_deleted(table, rws)
        conn.execute("COMMIT")
        counts["rows_stashed"] = counts.get("rows_stashed", 0) + sum(len(v) for v in rows.values())
    except sqlite3.Error as e:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        errors.append({"phase": "db-stash", "path": DB_MAIN, "reason": str(e)})
        counts["failed"] = counts.get("failed", 0) + 1
    finally:
        conn.close()
    return rows


def _db_apply_rows(db_path: Path, rows: Dict[str, List[Dict]], policy: str,
                   journal: Optional[RollbackJournal], dry: bool,
                   counts: Dict[str, int], errors: List[Dict]) -> None:
    """把切片中的行写回主库 (列交集 + 冲突策略)"""
    if not rows:
        return
    if not os.path.isfile(fs_path(db_path)):
        errors.append({"phase": "db-restore", "path": DB_MAIN, "reason": "目标数据库不存在"})
        counts["failed"] = counts.get("failed", 0) + 1
        return
    if dry:
        counts["rows_restored"] = counts.get("rows_restored", 0) + sum(len(v) for v in rows.values())
        return
    try:
        conn = sqlite3.connect(str(db_path))
        conn.isolation_level = None
    except sqlite3.Error as e:
        errors.append({"phase": "db-restore", "path": DB_MAIN, "reason": str(e)})
        counts["failed"] = counts.get("failed", 0) + 1
        return
    inserted = 0
    try:
        conn.execute("BEGIN")
        for table, rws in rows.items():
            cols = set(_table_columns(conn, table))
            if not cols:
                counts["skipped"] = counts.get("skipped", 0) + len(rws)
                errors.append({"phase": "db-restore", "path": table,
                               "reason": "目标库缺少该表"})
                continue
            for row in rws:
                keep = {k: v for k, v in row.items() if k in cols}
                if not keep:
                    continue
                pkc = _pk_col(conn, table)
                pk_val = keep.get(pkc) if pkc else None
                existed = False
                if pkc and pk_val is not None:
                    try:
                        existed = conn.execute(
                            "SELECT 1 FROM %s WHERE %s=?" % (table, pkc), (pk_val,)).fetchone() is not None
                    except sqlite3.Error:
                        existed = False
                if existed:
                    if policy == "keep":
                        counts["skipped"] = counts.get("skipped", 0) + 1
                        continue
                    if policy == "overwrite":
                        old = None
                        if journal is not None:
                            try:
                                cols_now = _table_columns(conn, table)
                                old_row = conn.execute(
                                    "SELECT * FROM %s WHERE %s=?" % (table, pkc),
                                    (pk_val,)).fetchone()
                                if old_row is not None:
                                    old = dict(zip(cols_now, old_row))
                            except sqlite3.Error:
                                old = None
                        sets = ",".join("%s=?" % c for c in keep)
                        try:
                            conn.execute("UPDATE %s SET %s WHERE %s=?" % (table, sets, pkc),
                                         list(keep.values()) + [pk_val])
                            if journal is not None and old:
                                journal.db_row_overwritten(table, str(pk_val), old)
                        except sqlite3.Error as e:
                            errors.append({"phase": "db-restore",
                                           "path": "%s:%s" % (table, pk_val), "reason": str(e)})
                            counts["failed"] = counts.get("failed", 0) + 1
                            continue
                        inserted += 1
                        continue
                    # rename -> 跳过 (行级改名会破坏外键引用)
                    counts["rows_skipped"] = counts.get("rows_skipped", 0) + 1
                    continue
                try:
                    conn.execute("INSERT OR IGNORE INTO %s (%s) VALUES (%s)"
                                 % (table, ",".join(keep), ",".join(["?"] * len(keep))),
                                 list(keep.values()))
                    inserted += 1
                    if journal is not None and pkc and pk_val is not None:
                        journal.db_rows_inserted(table, [str(pk_val)],
                                                 {str(pk_val): _row_sha_from(keep)})
                except sqlite3.Error as e:
                    errors.append({"phase": "db-restore",
                                   "path": "%s:%s" % (table, pk_val), "reason": str(e)})
                    counts["failed"] = counts.get("failed", 0) + 1
        conn.execute("COMMIT")
    except sqlite3.Error as e:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        errors.append({"phase": "db-restore", "path": DB_MAIN, "reason": str(e)})
        counts["failed"] = counts.get("failed", 0) + 1
    finally:
        conn.close()
    counts["rows_restored"] = counts.get("rows_restored", 0) + inserted


def _row_sha_from(row_map: Dict) -> str:
    h = hashlib.sha256()
    for k in sorted(row_map):
        h.update(("%s=%s;" % (k, row_map[k])).encode("utf-8", "replace"))
    return h.hexdigest()


def _write_account_meta(wb_home: Path, uid: str, extra: Dict) -> Optional[Path]:
    d = account_dir(wb_home, uid)
    try:
        _mkdir(d)
    except OSError:
        return None
    meta = {"user_id": uid, "updated_at": now_iso()[0], "tool_version": VERSION}
    meta.update(extra)
    p = d / ACCOUNT_META_FILE
    old = read_json(p)
    if isinstance(old, dict):
        hist = old.get("history") or []
        hist.append({"at": meta["updated_at"], "op": extra.get("last_op", "unknown")})
        meta["history"] = hist[-20:]
    write_json(p, meta)
    return p


def cmd_switch(opts: Dict) -> int:
    """同设备多账号切换"""
    wb_home: Path = opts["wb_home"]
    to_uid = (opts.get("to") or "").strip()
    cfg_policy = opts.get("config_policy") or "keep"
    db_mode = opts.get("db_mode") or "keep"
    dry = bool(opts.get("dry_run"))
    auto_rollback = bool(opts.get("auto_rollback", True))
    if not wb_home.is_dir():
        _LOG.error("WorkBuddy 数据目录不存在: %s" % wb_home)
        return EXIT_ERROR

    prog = Progress(9, "account-switch")
    prog.phase("环境与账号解析")
    if not dry:
        if is_windows() and check_app_running():
            _LOG.error("[占用] WorkBuddy 正在运行: Windows 下无法移动 %s 下的账号数据。"
                       "请完全退出 WorkBuddy (含托盘图标) 后重试。" % wb_home.name)
            return EXIT_PERMISSION
        if check_app_running():
            _LOG.warning("[占用] WorkBuddy 仍在运行, 建议退出后再切换 (--dry-run 可先预览)")
    accounts = discover_accounts(wb_home)
    if not accounts:
        _LOG.error("未识别到任何账号; 若目标账号尚未在本机登录过, "
                   "请先登录该账号或改用 restore 从备份包导入")
        return EXIT_ERROR
    uid_map = {a["user_id"]: a for a in accounts}
    if not to_uid:
        _LOG.error("请用 --to <USER_ID> 指定目标账号 (运行 `accounts` 查看清单)")
        return EXIT_ERROR
    if to_uid not in uid_map and _uid_from_name(to_uid):
        to_uid = _uid_from_name(to_uid)
    if to_uid not in uid_map:
        _LOG.error("目标账号不在本机账号清单中: %s" % to_uid)
        _LOG.error("同设备切换要求目标账号已在本机留下数据; "
                   "跨账号合并请改用: restore <backup.tar.gz> --auto-map")
        return EXIT_ERROR
    from_uid = (opts.get("from") or "").strip() or _latest_active_uid(accounts)
    if from_uid and from_uid not in uid_map:
        _LOG.error("源账号不存在: %s" % from_uid)
        return EXIT_ERROR
    if from_uid and from_uid == to_uid:
        _LOG.error("源账号与目标账号相同: %s (无需切换)" % to_uid)
        return EXIT_ERROR

    src_acc = uid_map.get(from_uid) if from_uid else None
    tgt_acc = uid_map[to_uid]
    _LOG.info("切换方向: %s -> %s" % (from_uid or "(未识别到的当前账号)", to_uid))
    _LOG.info("共享配置策略: %s   数据库模式: %s%s"
              % (cfg_policy, db_mode, "   [DRY-RUN 预览]" if dry else ""))
    _LOG.info("目标账号切片: %s (%d 文件 / %s)"
              % ("已存在" if tgt_acc["sliced"] else "不存在(将创建空切片)",
                 tgt_acc["files"], human_size(tgt_acc["bytes"])))

    # ---- 回滚记录 ----
    prog.phase("初始化回滚快照")
    switch_id = "switch-%s-%s" % (now_ts(), random.randint(1000, 9999))
    history_dir = history_root(wb_home) / switch_id
    files_backup_dir = history_dir / "files"
    if dry:
        files_backup_dir = None          # dry-run 真零写入: 落盘记录也不产生
    else:
        _mkdir(files_backup_dir)
    journal = RollbackJournal(
        history_dir, wb_home,
        {"format_version": BACKUP_FORMAT_VERSION,
         "source": {"wb_home": str(wb_home), "user_ids": [a["user_id"] for a in accounts]}},
        cfg_policy, list(MODULE_IDS))
    journal.meta["operation"] = "account-switch"
    journal.meta["switch"] = {"from": from_uid, "to": to_uid,
                              "config_policy": cfg_policy, "db_mode": db_mode}
    db_main = wb_home / DB_MAIN
    db_snapshot = None
    if db_main.is_file() and not dry:
        snap = history_dir / (DB_MAIN + ".pre")
        if _copy_sqlite_snapshot(db_main, snap) or shutil.copy2(fs_path(db_main), fs_path(snap)):
            db_snapshot = snap
    if not dry:
        journal.save(db_backup=db_snapshot)

    report = {
        "switch_id": switch_id, "dry_run": dry,
        "from": from_uid, "to": to_uid,
        "config_policy": cfg_policy, "db_mode": db_mode,
        "counts": {}, "errors": [], "skipped": [],
        "started_at": now_iso()[0],
    }
    counts: Dict[str, int] = {}
    errors: List[Dict] = []
    try:
        # ---- 迁移前备份当前账号 ----
        prog.phase("备份当前账号数据")
        if src_acc is not None:
            stats: Dict[str, int] = {}
            pre_dir = history_dir / ("pre-switch-account-" + _wsafe_component(from_uid))
            if dry:
                # 仅统计, 不落盘
                _dir_stats_touch(wb_home, src_acc["exclusive"], stats)
                _dir_stats_touch(wb_home, shared_config_files(), stats)
            else:
                _copy_rels(wb_home, src_acc["exclusive"], pre_dir, stats)
                _copy_rels(wb_home, shared_config_files(),
                           pre_dir / ACCOUNT_SHARED_DIRNAME, stats)
            if not dry:
                rows = _table_has_rows_for(db_main, from_uid)
                if rows:
                    _write_rows_json(pre_dir, rows)
                    counts["pre_backup_rows"] = sum(len(v) for v in rows.values())
            counts["pre_backup_files"] = stats.get("files", 0)
            _LOG.info("    已备份 %d 个文件, %d 行到 %s"
                      % (stats.get("files", 0), counts.get("pre_backup_rows", 0),
                         pre_dir.name))
        else:
            _LOG.info("    未识别到源账号, 跳过账号级备份")

        # ---- 切片: 当前账号移入隔离区 ----
        prog.phase("当前账号数据切片（移入隔离区）")
        if src_acc is not None and src_acc["exclusive"]:
            slot = account_dir(wb_home, from_uid)
            slot_data = slot / ACCOUNT_DATA_DIRNAME
            slot_shared = slot / ACCOUNT_SHARED_DIRNAME
            stash_counts: Dict[str, int] = {}
            if not dry:
                _mkdir(slot)
            _move_to_slot(wb_home, src_acc["exclusive"], slot_data,
                          journal, dry, stash_counts, errors)
            counts["stash_moved"] = stash_counts.get("moved", 0)
            counts["stash_skipped"] = stash_counts.get("skipped", 0)
            # 共享配置副本
            copy_stats: Dict[str, int] = {}
            if not dry:
                _copy_rels(wb_home, shared_config_files(), slot_shared, copy_stats)
            counts["shared_snapshot"] = copy_stats.get("files", 0) if not dry \
                else len([f for f in shared_config_files()
                          if (wb_home / f).is_file()])
            _LOG.info("    切片 %d 项, 跳过 %d 项"
                      % (counts["stash_moved"], counts["stash_skipped"]))
            if db_mode == "stash" and src_acc and not dry:
                # 注意: 必须先抽取再删除, 删除后无法再查回, 因此直接使用返回值
                stashed_rows = _db_stash_uid(db_main, from_uid, journal, dry,
                                             counts, errors)
                if stashed_rows:
                    _write_rows_json(slot, stashed_rows)
                    _LOG.info("    数据库行已抽取 %d 行 (%d 张表)"
                              % (counts.get("rows_stashed", 0), len(stashed_rows)))
                else:
                    _LOG.info("    数据库中无该账号数据")
        else:
            _LOG.info("    无可切片数据")

        # ---- 还原目标账号 ----
        prog.phase("还原目标账号数据")
        tgt_slot_data = account_dir(wb_home, to_uid) / ACCOUNT_DATA_DIRNAME
        if tgt_slot_data.is_dir():
            ctx = RestoreContext(
                wb_home,
                {"format_version": BACKUP_FORMAT_VERSION,
                 "platform": {"os": platform.system()},
                 "source": {"home_prefix": str(wb_home.parent), "user_ids": [to_uid]},
                 "options": {"modules": list(MODULE_IDS)}},
                {"conflict": cfg_policy, "restore_modules": None, "dry_run": dry,
                 "auto_map": False, "target_user_id": to_uid, "no_backup": True,
                 "allow_unknown": False, "all_tables": False,
                 "create_missing_tables": False})
            if from_uid:
                ctx.source_user_ids = [to_uid]
            ctx.ts_suffix = "restored-" + now_ts()
            ctx.journal = journal
            restore_counts: Dict[str, int] = {}
            restore_module_dir(tgt_slot_data, ctx, files_backup_dir, restore_counts)
            for k, v in restore_counts.items():
                counts["restore_" + k] = v
            total_restored = sum(v for k, v in restore_counts.items() if k.startswith("dry-")
                                 or k in ("created", "overwritten", "renamed", "skipped"))
            _LOG.info("    还原动作: %s"
                      % ", ".join("%s=%d" % (k, v) for k, v in sorted(restore_counts.items()))
                      or "无")
            # 注意: 此处不能清空目标切片 —— 必须等整个切换成功后再清,
            # 否则中途失败回滚时, 活动位置副本被撤销、切片已清空, 数据会两头皆空
        else:
            _LOG.info("    目标账号无历史切片, 已切换到干净环境")

        # ---- 共享配置冲突处理 ----
        prog.phase("共享配置冲突处理 (%s)" % cfg_policy)
        if from_uid:
            _shared_policy_apply(wb_home, cfg_policy, from_uid, to_uid, journal,
                                 files_backup_dir, dry, counts, errors)
            _LOG.info("    保留/覆盖/合并统计: skip=%d create=%d overwrite=%d merge=%d rename=%d"
                      % (counts.get("shared_skipped", 0), counts.get("created", 0),
                         counts.get("overwritten", 0), counts.get("merged", 0),
                         counts.get("renamed", 0)))

        # ---- 数据库层 ----
        prog.phase("数据库账号数据 (%s)" % db_mode)
        tgt_rows = _read_rows_json(account_dir(wb_home, to_uid))
        if db_mode == "keep":
            counts["db_kept"] = len(tgt_rows)
            _LOG.info("    keep 模式: 主库保持不变 (两账号数据共存, 仅文件系统层隔离)")
        elif db_mode == "merge":
            _db_apply_rows(db_main, tgt_rows, "keep", journal, dry, counts, errors)
            _LOG.info("    merge 模式: 补齐 %d 行 (已有行不修改)"
                      % counts.get("rows_restored", 0))
        elif db_mode == "stash":
            _db_apply_rows(db_main, tgt_rows, "overwrite", journal, dry, counts, errors)
            _LOG.info("    stash 模式: 还原 %d 行" % counts.get("rows_restored", 0))

        # ---- 元信息 ----
        prog.phase("写入切片元信息")
        if not dry:
            if from_uid:
                _write_account_meta(wb_home, from_uid,
                                    {"state": "stashed", "last_op": "stashed-out"})
            _write_account_meta(wb_home, to_uid,
                                {"state": "active", "last_op": "switched-in",
                                 "previous": from_uid})
    except InterruptedError_ as e:
        if not dry:
            journal.save(db_backup=db_snapshot)
        report["status"] = "interrupted"
        report["counts"] = counts
        report["errors"] = errors
        _LOG.warning("[!] 切换中断, 已保留回滚记录 %s" % switch_id)
        if auto_rollback and not dry:
            _LOG.warning("正在自动回滚...")
            cmd_rollback({"wb_home": wb_home, "id": switch_id, "dry_run": False,
                          "list": False, "latest": False, "hard": False,
                          "prune": False, "keep": 5})
        if opts.get("report_file"):
            _write_report(Path(opts["report_file"]), report)
        else:
            _write_report(history_dir / SWITCH_REPORT_FILE, report)
        return EXIT_INTERRUPTED
    except Exception as e:                                   # noqa: BLE001
        if not dry:
            journal.save(db_backup=db_snapshot)
        report["status"] = "failed"
        report["error"] = "%s: %s" % (type(e).__name__, e)
        report["counts"] = counts
        report["errors"] = errors
        _LOG.error("[失败] 切换过程异常: %s: %s" % (type(e).__name__, e))
        if auto_rollback and not dry:
            _LOG.error("正在自动回滚到切换前状态...")
            try:
                cmd_rollback({"wb_home": wb_home, "id": switch_id, "dry_run": False,
                              "list": False, "latest": False, "hard": False,
                              "prune": False, "keep": 5})
                report["auto_rolled_back"] = True
                _LOG.error("已回滚: %s" % switch_id)
            except Exception as rb:                          # noqa: BLE001
                report["auto_rolled_back"] = False
                _LOG.error("自动回滚失败, 请手动执行: rollback --id %s" % switch_id)
                _LOG.error("  %s" % rb)
        else:
            _LOG.error("如需回滚请执行: rollback --id %s" % switch_id)
        _write_report(history_dir / SWITCH_REPORT_FILE, report)
        if opts.get("report_file"):
            _write_report(Path(opts["report_file"]), report)
        return EXIT_ERROR

    # 全程无异常, 才清空目标切片 (数据已回到活动位置, 下次切走时会重新切片)
    if not dry and tgt_slot_data.is_dir():
        try:
            n_purged = 0
            for child in sorted(os.listdir(fs_path(tgt_slot_data))):
                cp = tgt_slot_data / child
                if cp.is_dir():
                    rmtree_safe(cp)
                else:
                    os.remove(fs_path(cp))
                n_purged += 1
            if n_purged:
                counts["slot_purged"] = n_purged
        except OSError as e:
            _LOG.debug("清空目标切片失败 (可忽略): %s" % e)
    if not dry:
        journal.save(db_backup=db_snapshot)
    prog.phase("完成")
    report["status"] = "ok" if not errors else "ok-with-warnings"
    report["counts"] = counts
    report["errors"] = errors
    report["finished_at"] = now_iso()[0]
    rep_path = history_dir / SWITCH_REPORT_FILE
    if dry:
        rep_path = None
    else:
        _write_report(rep_path, report)
    if opts.get("report_file"):
        _write_report(Path(opts["report_file"]), report)

    _LOG.info("")
    _LOG.info("== 切换完成: %s -> %s ==" % (from_uid or "(未知)", to_uid))
    _LOG.info("  成功项: 切片 %d 项 / 还原 %s / 写回行 %d"
              % (counts.get("stash_moved", 0),
                 counts.get("restore_created", 0) + counts.get("restore_overwritten", 0)
                 + counts.get("restore_renamed", 0) + counts.get("restore_dry-created", 0),
                 counts.get("rows_restored", 0)))
    _LOG.info("  跳过项: %d (同名未覆盖 / 目标已存在 / 非当前账号数据)"
              % (counts.get("shared_skipped", 0) + counts.get("stash_skipped", 0)
                 + counts.get("rows_skipped", 0)))
    if errors:
        _LOG.info("  失败项: %d" % len(errors))
        for e in errors[:10]:
            _LOG.info("    [失败] %s %s -> %s"
                      % (e.get("phase", "?"), e.get("path", "?"), e.get("reason", "?")))
        if len(errors) > 10:
            _LOG.info("    ... 其余 %d 项见报告" % (len(errors) - 10))
    _LOG.info("  隔离区: %s" % accounts_root(wb_home))
    _LOG.info("  回滚记录: %s (rollback --id %s)" % (switch_id, switch_id))
    if rep_path:
        _LOG.info("  报告文件: %s" % rep_path)
    if not dry:
        _LOG.info("  建议重启 WorkBuddy 使账号数据生效")
    return EXIT_OK


def _write_report(path: Path, report: Dict) -> Optional[Path]:
    try:
        _mkdir(path.parent)
        write_json(path, report)
        return path
    except OSError:
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wb-migrate",
        description="WorkBuddy 全局备份与迁移工具 v%s (备份/恢复/跨平台迁移/回滚/脱敏)" % VERSION,
        epilog="全局参数须放在子命令之前, 例如: wb-migrate --wb-home /path scan")
    p.add_argument("--version", action="version", version="wb-migrate %s" % VERSION)
    p.add_argument("--wb-home", default=None,
                   help="WorkBuddy 数据目录 (默认 ~/.workbuddy, 或环境变量 WB_MIGRATE_HOME)")
    p.add_argument("-v", "--verbose", action="store_true", help="调试输出")
    p.add_argument("-q", "--quiet", action="store_true", help="仅输出警告与错误")

    sub = p.add_subparsers(dest="command", metavar="<command>")

    sp = sub.add_parser("scan", help="扫描当前数据概览")
    sp.add_argument("--modules", default=None, help="仅统计指定模块 (逗号分隔)")
    sp.add_argument("--include-large", action="store_true", help="包含大目录")

    sp = sub.add_parser("modules", help="列出所有可用模块")
    sp.add_argument("--json", action="store_true", help="JSON 输出")

    sp = sub.add_parser("backup", help="创建备份包")
    sp.add_argument("-o", "--output", default=None, help="输出目录 (默认当前目录)")
    sp.add_argument("--modules", default=None, help="仅备份指定模块 (逗号分隔)")
    sp.add_argument("--exclude-modules", default=None, help="排除指定模块")
    sp.add_argument("--exclude", action="append", default=[],
                    help="排除路径 glob (相对 ~/.workbuddy, 可多次)")
    sp.add_argument("--include-large", action="store_true",
                    help="纳入 traces/ 等大目录")
    sp.add_argument("--include-other", action="store_true",
                    help="纳入未归类目录")
    sp.add_argument("--include-self", action="store_true",
                    help="包含 wb-migrate 技能自身 (默认排除)")
    sp.add_argument("--no-redact", action="store_true",
                    help="关闭敏感信息脱敏 (包内含明文凭据)")
    sp.add_argument("--level", type=int, default=6, help="gzip 压缩级别 1-9 (默认 6)")
    sp.add_argument("--dry-run", action="store_true", help="仅预览")
    sp.add_argument("--resume", default=None, metavar="BACKUP_ID", help="续传中断的备份")

    sp = sub.add_parser("info", help="查看备份包信息")
    sp.add_argument("archive", help="备份包路径")
    sp.add_argument("--verify", action="store_true", help="校验完整性 (sha256)")

    sp = sub.add_parser("verify", help="仅校验备份包完整性")
    sp.add_argument("archive", help="备份包路径")

    for name, helpstr in (("restore", "恢复备份包"), ("migrate", "迁移 (同 restore)")):
        sp = sub.add_parser(name, help=helpstr)
        sp.add_argument("archive", help="备份包路径")
        sp.add_argument("--conflict", choices=["merge", "overwrite", "rename"],
                        default="merge",
                        help="冲突策略: merge=跳过已有(默认) overwrite=覆盖 rename=改名导入")
        sp.add_argument("--modules", default=None,
                        help="仅恢复指定模块 (逗号分隔, 默认包内全部)")
        sp.add_argument("--dry-run", action="store_true", help="影子恢复预览 (零写入)")
        sp.add_argument("--auto-map", action="store_true",
                        help="自动映射路径前缀 + 合并账号 user_id")
        sp.add_argument("--target-user-id", default=None, metavar="UUID",
                        help="显式指定目标账号 user_id")
        sp.add_argument("--no-backup", action="store_true",
                        help="跳过恢复前快照 (不推荐)")
        sp.add_argument("--allow-unknown", action="store_true",
                        help="旧包未知顶层条目放行")
        sp.add_argument("--all-tables", action="store_true",
                        help="忽略模块过滤, 合并全部数据库表")
        sp.add_argument("--create-missing-tables", action="store_true",
                        help="目标库缺表时按源 DDL 自动建表")

    sp = sub.add_parser("accounts", help="列出本机账号 (同设备多账号切换)")
    sp.add_argument("--json", action="store_true", help="输出 JSON")

    sp = sub.add_parser("switch", help="同设备多账号切换 (Account A -> B)")
    sp.add_argument("--to", required=True, metavar="USER_ID", help="目标账号 user_id")
    sp.add_argument("--from", dest="from_uid", default=None, metavar="USER_ID",
                    help="源账号 user_id (默认推断当前活跃账号)")
    sp.add_argument("--config-policy", choices=["keep", "overwrite", "merge"],
                    default="keep", help="同名共享配置冲突策略 (默认 keep)")
    sp.add_argument("--db-mode", choices=["keep", "merge", "stash"], default="keep",
                    help="数据库账号数据处理方式 (默认 keep: 不动主库)")
    sp.add_argument("--dry-run", action="store_true", help="预览, 不改动任何数据")
    sp.add_argument("--no-auto-rollback", action="store_true",
                    help="关闭失败自动回滚 (保留现场用于排查)")
    sp.add_argument("--report-file", default=None, help="额外输出报告 JSON 到指定路径")

    sp = sub.add_parser("compat", help="环境兼容性自检报告")
    sp.add_argument("--json", action="store_true", help="输出 JSON")

    sp = sub.add_parser("selftest", help="跨平台端到端一致性自检")

    sp = sub.add_parser("rollback", help="回滚一次恢复")
    sp.add_argument("--list", action="store_true", help="列出可回滚记录")
    sp.add_argument("--latest", action="store_true", help="回滚最近一次")
    sp.add_argument("--id", default=None, help="指定 rollback_id")
    sp.add_argument("--dry-run", action="store_true", help="预览回滚")
    sp.add_argument("--hard", action="store_true",
                    help="硬回滚: 用快照整体替换数据库")
    sp.add_argument("--prune", action="store_true", help="清理历史快照")
    sp.add_argument("--keep", type=int, default=5, help="prune 时保留数量 (默认 5)")

    return p


def main(argv: Optional[List[str]] = None) -> int:
    # [v3.3.0] Windows 控制台默认 GBK: 中文路径/日志经管道时会抛
    # UnicodeDecodeError(0xd4 非合法 UTF-8 续字节) 或 UnicodeEncodeError。
    # 统一强制 UTF-8 且 errors=replace, 保证任何编码环境下都不因编码中断迁移。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_ERROR

    wb_home = resolve_wb_home(args.wb_home)
    if str(wb_home) != str(Path.home() / ".workbuddy"):
        _LOG.info("[TEST] 使用非默认数据目录: %s" % wb_home)
    log_path = setup_logging(wb_home, args.verbose, args.quiet)
    if log_path:
        _LOG.debug("日志文件: %s" % log_path)
    install_signal_handlers()

    try:
        if args.command == "scan":
            cmd_scan({"wb_home": wb_home,
                      "modules": resolve_modules(_split_list(args.modules)),
                      "include_large": args.include_large,
                      "include_self": False})
            return EXIT_OK
        if args.command == "modules":
            cmd_modules(args.json)
            return EXIT_OK
        if args.command == "backup":
            opts = {
                "wb_home": wb_home,
                "modules": resolve_modules(_split_list(args.modules),
                                           _split_list(args.exclude_modules)),
                "output": Path(args.output).expanduser() if args.output else Path.cwd(),
                "exclude_patterns": list(args.exclude),
                "include_large": args.include_large,
                "include_other": args.include_other,
                "include_self": args.include_self,
                "redact": not args.no_redact,
                "level": max(1, min(9, args.level)),
                "dry_run": args.dry_run,
                "resume": args.resume,
            }
            create_backup(opts)
            return EXIT_OK
        if args.command in ("info", "verify"):
            opts = {"archive": Path(args.archive).expanduser(),
                    "verify": getattr(args, "verify", False) or args.command == "verify"}
            return cmd_info(opts)
        if args.command in ("restore", "migrate"):
            if args.command == "migrate":
                _LOG.info("[提示] migrate 为 restore 的别名")
            opts = {
                "wb_home": wb_home,
                "archive": Path(args.archive).expanduser(),
                "conflict": args.conflict,
                "restore_modules": _split_list(args.modules),
                "dry_run": args.dry_run,
                "auto_map": args.auto_map,
                "target_user_id": args.target_user_id,
                "no_backup": args.no_backup,
                "allow_unknown": args.allow_unknown,
                "all_tables": args.all_tables,
                "create_missing_tables": args.create_missing_tables,
            }
            return restore_backup(opts["archive"], opts)
        if args.command == "accounts":
            return cmd_accounts({"wb_home": wb_home, "json": args.json})
        if args.command == "switch":
            return cmd_switch({"wb_home": wb_home, "to": args.to, "from": args.from_uid,
                               "config_policy": args.config_policy, "db_mode": args.db_mode,
                               "dry_run": args.dry_run,
                               "auto_rollback": not args.no_auto_rollback,
                               "report_file": args.report_file})
        if args.command == "compat":
            rep = build_compat_report(wb_home)
            if getattr(args, "json", False):
                print(json.dumps(rep, ensure_ascii=False, indent=2))
                return EXIT_OK if rep["ok"] else EXIT_ERROR
            return cmd_compat({"wb_home": wb_home})
        if args.command == "selftest":
            return run_selftest({"wb_home": wb_home})
        if args.command == "rollback":
            if not (args.list or args.latest or args.id or args.prune):
                _LOG.error("请指定 --list / --latest / --id / --prune 之一")
                return EXIT_ERROR
            return cmd_rollback({"wb_home": wb_home, "list": args.list,
                                 "latest": args.latest, "id": args.id,
                                 "dry_run": args.dry_run, "hard": args.hard,
                                 "prune": args.prune, "keep": args.keep})
        parser.print_help()
        return EXIT_ERROR
    except ChecksumError as e:
        _LOG.error("[校验失败] %s" % e)
        return EXIT_CHECKSUM
    except PermissionError as e:
        _LOG.error("[权限不足] %s" % e)
        return EXIT_PERMISSION
    except InterruptedError_ as e:
        _LOG.error("[中断] %s" % e)
        _LOG.error("备份中断: staging 已保留, 可用 --resume <BACKUP_ID> 续传; "
                   "恢复中断: 可用 rollback --latest 撤销已应用的改动")
        return EXIT_INTERRUPTED
    except FileNotFoundError as e:
        _LOG.error("[错误] %s" % e)
        return EXIT_ERROR
    except KeyboardInterrupt:
        _LOG.error("[中断] 用户终止")
        return EXIT_INTERRUPTED
    except Exception as e:
        import traceback
        _LOG.error("[错误] %s" % e)
        _LOG.debug(traceback.format_exc())
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
