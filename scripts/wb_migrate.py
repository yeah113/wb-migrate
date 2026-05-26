#!/usr/bin/env python3
"""
wb-migrate — WorkBuddy 数据迁移工具 v2.0.3
支持跨平台 (macOS / Windows / Linux) 的备份、恢复和迁移

用法:
    python3 wb_migrate.py scan                          # 扫描当前数据
    python3 wb_migrate.py backup  [--output DIR]        # 创建备份
    python3 wb_migrate.py info    <archive.tar.gz>       # 查看备份信息
    python3 wb_migrate.py restore <archive.tar.gz>      # 恢复备份 (智能合并)
    python3 wb_migrate.py restore <archive.tar.gz> --dry-run   # 预览
    python3 wb_migrate.py restore <archive.tar.gz> --force     # 强制覆盖
    python3 wb_migrate.py restore <archive.tar.gz> --auto-map   # 自动路径/user_id 映射
    python3 wb_migrate.py restore <archive.tar.gz> --auto-map --target-user-id <uuid>
"""

import os
import sys
import io
import json
import shutil
import sqlite3
import tarfile
import hashlib
import tempfile
import argparse
import platform
import datetime
from pathlib import Path, PureWindowsPath
from typing import Optional, Dict, List, Tuple, Set

VERSION = "2.0.3"

# ============================================================
# 平台检测与路径
# ============================================================

def get_wb_home() -> Path:
    """返回 WorkBuddy 数据目录"""
    return Path.home() / ".workbuddy"


def get_platform_info() -> Dict[str, str]:
    """获取当前平台信息"""
    return {
        "os": platform.system(),           # Darwin / Windows / Linux
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "username": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "hostname": platform.node(),
        "home": str(Path.home()),
    }


def is_windows() -> bool:
    return platform.system() == "Windows"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_linux() -> bool:
    return platform.system() == "Linux"


# ============================================================
# 跨平台路径映射引擎
# ============================================================

# 各平台的 home 目录范式
OS_HOME_PATTERNS = {
    "Darwin":  "/Users/{username}",
    "Windows": "{drive}:\\Users\\{username}",
    "Linux":   "/home/{username}",
}

# 路径前缀探测优先级
HOME_CANDIDATES = [
    "/Users/",        # macOS
    "/home/",         # Linux
]


def detect_source_home_prefix(meta: Dict) -> Optional[str]:
    """从备份元数据中检测源平台的 home 目录前缀"""
    source_os = meta.get("platform", {}).get("os", "")
    source_user = meta.get("platform", {}).get("username", "")
    source_path = meta.get("source_path", "")

    if source_user:
        if source_os == "Darwin" or source_os == "Linux":
            prefix = f"{source_path}"
            # 从 source_path 推导: /Users/<name>/.workbuddy -> /Users/<name>
            if "/.workbuddy" in prefix:
                prefix = prefix[:prefix.rfind("/.workbuddy")]
            elif prefix.endswith("/.workbuddy"):
                prefix = prefix[:-len("/.workbuddy")]
            return prefix

        elif source_os == "Windows":
            # C:\Users\<name>\.workbuddy -> C:\Users\<name>
            if "\\.workbuddy" in source_path:
                return source_path[:source_path.rfind("\\.workbuddy")]
            elif source_path.endswith("\\.workbuddy"):
                return source_path[:-len("\\.workbuddy")]
            return source_path

    # 回退：使用常见的 home 模式
    if source_os == "Darwin":
        return f"/Users/{source_user}" if source_user else None
    elif source_os == "Linux":
        return f"/home/{source_user}" if source_user else None
    elif source_os == "Windows":
        return f"C:\\Users\\{source_user}" if source_user else "C:\\Users"

    return None


def get_target_home_prefix() -> str:
    """获取当前平台的 home 目录"""
    return str(Path.home())


def needs_path_remapping(meta: Dict) -> bool:
    """判断是否需要跨平台路径映射"""
    source_os = meta.get("platform", {}).get("os", "")
    target_os = platform.system()
    return source_os and source_os != target_os


def remap_path(path: str, source_prefix: str, target_prefix: str) -> str:
    """将路径从源平台格式映射到目标平台格式"""
    if not path:
        return path

    # 规范化分隔符进行比较
    norm_path = path.replace("\\", "/")
    norm_src = source_prefix.replace("\\", "/")

    if norm_path.startswith(norm_src):
        relative = norm_path[len(norm_src):].lstrip("/")
        # 用目标平台的路径分隔符
        if is_windows():
            return os.path.join(target_prefix, relative.replace("/", "\\"))
        else:
            return os.path.join(target_prefix, relative)

    # 如果路径不以 source_prefix 开头，尝试更宽松的匹配
    norm_target = target_prefix.replace("\\", "/")
    src_user = os.path.basename(norm_src)
    tgt_user = os.path.basename(norm_target)

    # 替换用户名
    if src_user and tgt_user and src_user != tgt_user:
        replaced = norm_path.replace(f"/{src_user}/", f"/{tgt_user}/")
        if is_windows():
            return replaced.replace("/", "\\")
        return replaced

    return path


def join_mapped_path(target_prefix: str, relative: str) -> str:
    """按目标平台规范拼接映射后的相对路径。"""
    if is_windows():
        relative = relative.replace("/", "\\")
    else:
        relative = relative.replace("\\", "/")
    return os.path.join(target_prefix, relative)


def cwd_to_project_dir(cwd: str) -> str:
    """将 cwd 路径编码为 WorkBuddy projects 目录名"""
    if not cwd:
        return ""
    normalized = cwd.replace("\\", "/")
    if ":" in normalized:
        # Windows: C:/Users/... → c-Users-...
        drive = normalized.split(":")[0].lower()
        rest = normalized.split(":", 1)[1].lstrip("/")
        return drive + "-" + rest.replace("/", "-")
    else:
        # Unix: /Users/alice/... → Users-alice-...
        rest = normalized.lstrip("/")
        return rest.replace("/", "-")



# ============================================================
# 备份清单
# ============================================================

BACKUP_MANIFEST = {
    "database": {
        "type": "file", "path": "workbuddy.db", "required": True,
        "description": "SQLite 数据库 (sessions, automations, workspaces)",
    },
    "skills_user": {
        "type": "dir", "path": "skills", "required": False,
        "description": "用户级 Skills",
        "exclude": ["_bm_skillid_migration.json"],
    },
    "projects": {
        "type": "dir", "path": "projects", "required": False,
        "description": "项目工作区数据 (对话历史, memory, skills)",
    },
    "connectors": {
        "type": "dir", "path": "connectors", "required": False,
        "description": "MCP 连接器配置",
    },
    "sessions": {
        "type": "dir", "path": "sessions", "required": False,
        "description": "活跃会话状态",
    },
    "local_storage": {
        "type": "dir", "path": "local_storage", "required": False,
        "description": "本地存储键值",
    },
    "traces": {
        "type": "dir", "path": "traces", "required": False,
        "description": "执行追踪日志",
    },
    "tasks": {
        "type": "dir", "path": "tasks", "required": False,
        "description": "任务列表",
    },
    "teams": {
        "type": "dir", "path": "teams", "required": False,
        "description": "团队配置",
    },
    "blobs": {
        "type": "dir", "path": "blobs", "required": False,
        "description": "二进制对象",
    },
    "file_history": {
        "type": "dir", "path": "file-history", "required": False,
        "description": "文件版本历史",
    },
    "artifact_index": {
        "type": "dir", "path": "artifact-index", "required": False,
        "description": "Artifact 索引",
    },
    "media_index": {
        "type": "dir", "path": "media-index", "required": False,
        "description": "媒体索引",
    },
    "shell_snapshots": {
        "type": "dir", "path": "shell-snapshots", "required": False,
        "description": "Shell 快照",
    },
    "clipboard_images": {
        "type": "dir", "path": "clipboard-images", "required": False,
        "description": "剪贴板图片",
    },
    "identity_files": {
        "type": "files",
        "paths": [
            "BOOTSTRAP.md", "SOUL.md", "IDENTITY.md", "USER.md",
            "MEMORY.md", "models.json", "mcp.json", "argv.json",
            "workspace-state.json", "user-state.json",
            "expert-history.json", "mcp-approvals.json",
        ],
        "required": False,
        "description": "身份、配置与偏好文件",
    },
}

SKIP_DIRS_DEFAULT = [
    "binaries", "app", "plugins", "skills-marketplace",
    "connectors-marketplace", "plugin-marketplace-state",
    "plugin-marketplace-state-new", "extensions", "logs", "memery",
]

DB_TABLES = [
    ("sessions", "id"),
    ("automations", "id"),
    ("automation_runs", "thread_id"),
    ("automation_runtime_state", "automation_id"),
    ("workspaces", "path"),
    ("migration_meta", "key"),
]


# ============================================================
# 备份引擎
# ============================================================

def create_backup(output_dir: str = ".", include_binaries: bool = False) -> str:
    """创建 WorkBuddy 完整备份"""
    wb_home = get_wb_home()
    if not wb_home.exists():
        raise FileNotFoundError(f"WorkBuddy 数据目录不存在: {wb_home}")

    # ===== 沙箱权限预检测 =====
    output_path = Path(output_dir).resolve()
    required_paths = [wb_home, output_path]
    if not check_write_permissions(required_paths, label="备份"):
        raise PermissionError("沙箱权限不足，请放开权限后重试")

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    hostname = platform.node().split(".")[0] or "unknown"
    username = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
    filename = f"wb-backup-{username}-{hostname}-{timestamp}.tar.gz"
    output_path.mkdir(parents=True, exist_ok=True)
    archive_path = output_path / filename

    stats = {"files": 0, "dirs": 0, "size_bytes": 0, "errors": []}

    print(f"\n{'='*60}")
    print(f"  WorkBuddy 备份 v{VERSION}")
    print(f"  源目录: {wb_home}")
    print(f"  目标:   {archive_path}")
    print(f"{'='*60}\n")

    with tarfile.open(archive_path, "w:gz") as tar:
        # 元数据
        meta = {
            "version": VERSION,
            "tool": "wb-migrate",
            "timestamp": datetime.datetime.now().isoformat(),
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "platform": get_platform_info(),
            "backup_type": "full" if include_binaries else "standard",
            "source_path": str(wb_home),
        }
        meta_bytes = json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8")
        meta_info = tarfile.TarInfo("metadata.json")
        meta_info.size = len(meta_bytes)
        meta_info.mtime = int(datetime.datetime.now().timestamp())
        tar.addfile(meta_info, io.BytesIO(meta_bytes))
        print("  [OK] metadata.json")

        # 数据库
        db_path = wb_home / "workbuddy.db"
        if db_path.exists():
            with tempfile.TemporaryDirectory(prefix="wb_db_snapshot_") as snap_dir:
                snapshot_path = Path(snap_dir) / "workbuddy.db"
                try:
                    _copy_sqlite_snapshot(db_path, snapshot_path)
                    db_to_add = snapshot_path
                    print("  [OK] workbuddy.db 一致性快照")
                except (sqlite3.Error, OSError) as e:
                    db_to_add = db_path
                    print(f"  [WARN] SQLite 快照失败，改用原始数据库文件: {e}")

                db_size = db_to_add.stat().st_size
                tar.add(db_to_add, arcname="workbuddy.db")
            stats["files"] += 1
            stats["size_bytes"] += db_size
            print(f"  [OK] workbuddy.db ({_fmt_size(db_size)})")

        # 目录
        for key, item in BACKUP_MANIFEST.items():
            if item["type"] == "dir":
                dir_path = wb_home / item["path"]
                if dir_path.exists() and dir_path.is_dir():
                    try:
                        _add_dir_to_tar(tar, dir_path, item["path"], stats,
                                       skip_patterns=item.get("exclude", []))
                        print(f"  [OK] {item['path']}/")
                    except Exception as e:
                        print(f"  [WARN] {item['path']}/: {e}")
                        stats["errors"].append(f"{item['path']}: {e}")
            elif item["type"] == "files":
                for fn in item.get("paths", []):
                    fp = wb_home / fn
                    if fp.exists() and fp.is_file():
                        try:
                            tar.add(fp, arcname=fn)
                            stats["files"] += 1
                            print(f"  [OK] {fn}")
                        except Exception as e:
                            stats["errors"].append(f"{fn}: {e}")

    archive_size = archive_path.stat().st_size
    print(f"\n{'='*60}")
    print(f"  备份完成")
    print(f"  文件数:     {stats['files']}")
    print(f"  压缩包:     {_fmt_size(archive_size)}")
    if stats["errors"]:
        print(f"  警告:       {len(stats['errors'])} 个")
    print(f"  路径:       {archive_path}")
    print(f"{'='*60}\n")
    return str(archive_path)


def _add_dir_to_tar(tar, dir_path, arcname, stats, skip_patterns=None):
    """递归添加目录到 tar"""
    skip_patterns = skip_patterns or []
    for item in dir_path.rglob("*"):
        rel = str(item.relative_to(dir_path))
        if any(rel == p or rel.startswith(p + "/") for p in skip_patterns):
            continue
        tar_path = f"{arcname}/{rel}"
        try:
            if item.is_file():
                tar.add(item, arcname=tar_path)
                stats["files"] = stats.get("files", 0) + 1
            elif item.is_dir():
                tar.add(item, arcname=tar_path, recursive=False)
                stats["dirs"] = stats.get("dirs", 0) + 1
        except (PermissionError, FileNotFoundError):
            pass


def _copy_sqlite_snapshot(source: Path, target: Path):
    """使用 SQLite backup API 创建一致性快照，包含 WAL 中尚未 checkpoint 的数据。"""
    source_conn = sqlite3.connect(str(source))
    target_conn = sqlite3.connect(str(target))
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


# ============================================================
# 备份信息查看
# ============================================================

def show_backup_info(archive_path: str) -> Dict:
    """展示备份包信息，返回元数据"""
    archive_size = Path(archive_path).stat().st_size

    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()
        meta = {}
        try:
            mf = tar.extractfile("metadata.json")
            if mf:
                meta = json.loads(mf.read().decode("utf-8"))
        except KeyError:
            pass

        print(f"\n{'='*60}")
        print(f"  备份包信息")
        print(f"{'='*60}")
        print(f"  文件:        {archive_path}")
        print(f"  大小:        {_fmt_size(archive_size)}")

        if meta:
            pi = meta.get("platform", {})
            print(f"\n  创建时间:    {meta.get('timestamp', 'N/A')}")
            print(f"  工具版本:    {meta.get('version', 'N/A')}")
            print(f"  源平台:      {pi.get('os', 'N/A')} {pi.get('os_release', '')}")
            print(f"  源用户:      {pi.get('username', 'N/A')}")
            print(f"  源主机:      {pi.get('hostname', 'N/A')}")
            print(f"  源 home:     {pi.get('home', 'N/A')}")
            print(f"  备份类型:    {meta.get('backup_type', 'N/A')}")

        # 模块统计
        modules = {}
        for m in members:
            mod = m.name.split("/")[0]
            if mod not in modules:
                modules[mod] = {"files": 0, "size": 0}
            if m.isfile():
                modules[mod]["files"] += 1
                modules[mod]["size"] += m.size

        print(f"\n  包含的模块:")
        for mod in sorted(modules.keys()):
            info = modules[mod]
            print(f"    {mod:<25s} {info['files']:>4d} 文件  {_fmt_size(info['size'])}")

        total_files = sum(m["files"] for m in modules.values())
        total_size = sum(m["size"] for m in modules.values())
        print(f"\n  总计:        {total_files} 文件, {_fmt_size(total_size)}")

        # 跨平台检测
        source_os = meta.get("platform", {}).get("os", "")
        current_os = platform.system()
        if source_os and source_os != current_os:
            print(f"\n  [跨平台] 源={source_os} → 目标={current_os}")
            print(f"  恢复时建议使用 --auto-map 自动映射路径")

        print(f"{'='*60}\n")
    return meta


# ============================================================
# 恢复引擎
# ============================================================

class RestoreContext:
    """恢复上下文，贯穿整个恢复流程"""

    def __init__(self, archive_path: str, dry_run: bool = False,
                 force: bool = False, auto_map: bool = False,
                 target_user_id: Optional[str] = None):
        self.archive_path = archive_path
        self.dry_run = dry_run
        self.force = force
        self.auto_map = auto_map
        self.target_user_id_explicit = bool(target_user_id)
        self.wb_home = get_wb_home()
        self.meta: Dict = {}
        self.source_os: str = ""
        self.target_os: str = platform.system()
        self.source_prefix: Optional[str] = None
        self.target_prefix: str = get_target_home_prefix()
        self.needs_remap: bool = False
        self.sessions_remapped: int = 0
        self.project_dirs_renamed: int = 0
        self.user_id_merged: int = 0
        self._source_user_id: Optional[str] = None
        self._source_user_ids: List[str] = []
        self._target_user_id: Optional[str] = target_user_id

    def detect_platform_mismatch(self):
        """检测跨平台场景并准备映射参数"""
        source_os = self.meta.get("platform", {}).get("os", "")
        self.source_os = source_os

        if source_os and source_os != self.target_os:
            self.needs_remap = True
            self.source_prefix = detect_source_home_prefix(self.meta)
            if self.auto_map:
                print(f"\n  [跨平台检测] 源={source_os} → 目标={self.target_os}")
                print(f"  源 home:  {self.source_prefix}")
                print(f"  目标 home: {self.target_prefix}")
                print(f"  将自动映射路径并迁移 user_id\n")
            elif not self.dry_run:
                print(f"\n  [跨平台检测] 备份来自 {source_os}, 当前为 {self.target_os}")
                print(f"  [提示] 使用 --auto-map 自动映射路径")
                print(f"  [提示] 否则 sessions 路径不匹配将无法使用\n")

    def detect_user_ids(self, source_conn: sqlite3.Connection, detect_target: bool = True):
        """检测源和目标的 user_id"""
        src_rows = source_conn.execute(
            "SELECT DISTINCT user_id FROM sessions WHERE user_id IS NOT NULL"
        ).fetchall()
        self._source_user_ids = [r[0] for r in src_rows if r[0]]
        if self._source_user_ids:
            self._source_user_id = self._source_user_ids[0]

        target_db = self.wb_home / "workbuddy.db"
        if detect_target and not self._target_user_id and target_db.exists():
            tgt_conn = sqlite3.connect(str(target_db))
            tgt_rows = _select_target_user_ids(tgt_conn)
            if tgt_rows:
                self._target_user_id = tgt_rows[0]
            tgt_conn.close()


def restore_backup(archive_path: str, dry_run: bool = False, force: bool = False,
                   auto_map: bool = False, target_user_id: Optional[str] = None) -> Dict:
    """从备份恢复 WorkBuddy 数据"""
    ctx = RestoreContext(archive_path, dry_run, force, auto_map, target_user_id)

    if not Path(archive_path).exists():
        raise FileNotFoundError(f"备份文件不存在: {archive_path}")

    # ===== 沙箱权限预检测 =====
    # 在执行任何写入操作前，先检测关键路径是否可写
    required_paths = [
        ctx.wb_home,                      # ~/.workbuddy 目录
        ctx.wb_home / "workbuddy.db",     # 数据库文件
        ctx.wb_home / "tasks",            # 任务目录
        ctx.wb_home / "projects",         # 项目目录
    ]
    if not check_write_permissions(required_paths, label="恢复"):
        raise PermissionError("沙箱权限不足，请放开权限后重试")

    if not dry_run:
        ctx.wb_home.mkdir(parents=True, exist_ok=True)

    # 读取元数据
    with tarfile.open(archive_path, "r:gz") as tar:
        try:
            mf = tar.extractfile("metadata.json")
            if mf:
                ctx.meta = json.loads(mf.read().decode("utf-8"))
        except KeyError:
            ctx.meta = {}

    # 跨平台检测
    ctx.detect_platform_mismatch()

    print(f"\n{'='*60}")
    print(f"  WorkBuddy 恢复 v{VERSION}")
    print(f"  备份:   {archive_path}")
    print(f"  目标:   {ctx.wb_home}")
    tags = []
    if dry_run: tags.append("DRY-RUN")
    if force: tags.append("FORCE")
    if auto_map: tags.append("AUTO-MAP")
    if tags: print(f"  模式:   {' | '.join(tags)}")
    print(f"{'='*60}\n")

    # 解压到临时目录
    with tempfile.TemporaryDirectory(prefix="wb_restore_") as tmpdir:
        tmp = Path(tmpdir)
        print("  解压备份...")
        with tarfile.open(archive_path, "r:gz") as tar:
            _safe_extract_tar(tar, tmp)
        print("  [OK] 解压完成\n")

        entries = [p for p in tmp.iterdir() if p.name != "metadata.json"]
        print(f"  发现 {len(entries)} 个数据模块\n")

        # 1. 恢复数据库（智能合并 + 跨平台映射）
        db_source = tmp / "workbuddy.db"
        if db_source.exists():
            _restore_database(db_source, ctx)
        else:
            print("  [WARN] 备份中无 workbuddy.db\n")

        # 2. 恢复目录
        for entry in entries:
            if entry.name in ("workbuddy.db", "metadata.json"):
                continue
            target = ctx.wb_home / entry.name
            if entry.is_dir():
                if dry_run:
                    _dry_list_dir(entry, target)
                else:
                    _restore_dir(entry, target, force)
            elif entry.is_file():
                if dry_run:
                    print(f"  [DRY-RUN] 文件: {entry.name}")
                else:
                    _restore_file(entry, target, force)

        # 3. 跨平台 projects 目录重命名 + 工作区目录创建
        if ctx.needs_remap and auto_map and not dry_run:
            print()
            _remap_project_dirs(ctx)
            _ensure_workspace_dirs(ctx)

    # 4. 恢复后校验
    validation_result = None
    if not dry_run:
        print()
        validation_result = validate_restore(ctx)
        if validation_result["passed"]:
            print(f"\n  [OK] 校验通过 - 所有数据完整可用")
        else:
            print(f"\n  [WARN] 校验发现 {len(validation_result['issues'])} 个问题:")
            for issue in validation_result["issues"]:
                print(f"    - {issue}")

    print(f"\n{'='*60}")
    if dry_run:
        print(f"  DRY-RUN 完成 — 未修改任何数据")
    else:
        print(f"  恢复完成!")
        if ctx.sessions_remapped:
            print(f"  路径映射:    {ctx.sessions_remapped} 条 session")
        if ctx.project_dirs_renamed:
            print(f"  目录重命名:  {ctx.project_dirs_renamed} 个")
        if ctx.user_id_merged:
            print(f"  user_id:     {ctx.user_id_merged} 条")
    print(f"{'='*60}\n")

    return {"ctx": ctx, "validation": validation_result if not dry_run else None}


# ============================================================
# 数据库恢复（合并 + 跨平台映射）
# ============================================================

def _restore_database(db_source: Path, ctx: RestoreContext):
    """智能合并 SQLite 数据库，支持跨平台路径映射"""
    db_target = ctx.wb_home / "workbuddy.db"
    print(f"  --- 数据库 ---")
    print(f"  源: {db_source} ({_fmt_size(db_source.stat().st_size)})")

    if not db_target.exists():
        if ctx.dry_run:
            print(f"  [DRY-RUN] 新建数据库\n")
            _preview_path_mapping(db_source, ctx)
            _preview_user_id_mapping(db_source, ctx, detect_target=False)
        else:
            source_conn = sqlite3.connect(str(db_source))
            try:
                ctx.detect_user_ids(source_conn, detect_target=False)
            finally:
                source_conn.close()

            db_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_source, db_target)
            print(f"  [OK] 新建数据库")
            if ctx.auto_map:
                target_conn = sqlite3.connect(str(db_target))
                try:
                    _apply_database_auto_map(target_conn, ctx, allow_user_merge=True)
                    target_conn.commit()
                finally:
                    target_conn.close()
            print()
        return

    print(f"  目标: {db_target} ({_fmt_size(db_target.stat().st_size)})")

    if ctx.dry_run:
        analysis = _analyze_db_diff(db_source, db_target)
        print(f"  [DRY-RUN] 合并预览:")
        for table, counts in analysis.items():
            if counts["new"] > 0:
                print(f"    {table}: +{counts['new']} 新记录, "
                      f"{counts['conflict']} 冲突(跳过)")
            else:
                print(f"    {table}: 无新记录")

        _preview_path_mapping(db_source, ctx)
        _preview_user_id_mapping(db_source, ctx, detect_target=True)
        print()
        return

    # 安全检查：备份目标数据库
    _backup_target_db(db_target)

    source_conn = sqlite3.connect(str(db_source))
    target_conn = sqlite3.connect(str(db_target))

    # 检测 user_id
    ctx.detect_user_ids(source_conn)

    # 执行合并
    merged = _merge_databases(source_conn, target_conn)
    for table, counts in merged.items():
        print(f"    {table}: +{counts['inserted']}, 跳过 {counts['skipped']}")

    if ctx.auto_map:
        _apply_database_auto_map(target_conn, ctx, allow_user_merge=True)

    target_conn.commit()
    source_conn.close()
    target_conn.close()

    print(f"  [OK] 数据库合并完成\n")


def _backup_target_db(db_target: Path):
    """在恢复前备份目标数据库"""
    backup_path = db_target.with_suffix(
        f".backup-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    try:
        shutil.copy2(db_target, backup_path)
        print(f"  [安全] 已备份目标数据库: {backup_path.name}")
    except Exception as e:
        print(f"  [WARN] 无法备份目标数据库: {e}")


def _merge_databases(source_conn, target_conn) -> Dict:
    """执行 SQLite 数据库合并
    NOTE: src_cursor.description[i] 返回 (name, type_code, ...)
    使用 [0] 获取列名
    """
    results = {}
    for table, pk_col in DB_TABLES:
        try:
            src_cursor = source_conn.execute(f"SELECT * FROM {table}")
            # cursor.description[i][0] = column name
            columns = [col[0] for col in src_cursor.description]
            rows = src_cursor.fetchall()
        except sqlite3.OperationalError:
            results[table] = {"inserted": 0, "skipped": 0, "error": "table not found"}
            continue

        inserted, skipped = 0, 0
        for row in rows:
            row_dict = dict(zip(columns, row))
            pk = row_dict.get(pk_col)

            if pk:
                exists = target_conn.execute(
                    f"SELECT 1 FROM {table} WHERE {pk_col}=?", (pk,)
                ).fetchone()
                if exists:
                    skipped += 1
                    continue

            placeholders = ", ".join(["?" for _ in columns])
            col_names = ", ".join(columns)
            values = [row_dict[c] for c in columns]

            try:
                target_conn.execute(
                    f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",
                    values
                )
                inserted += 1
            except sqlite3.Error:
                skipped += 1

        # 注意: 不在此处 commit，由调用方统一 commit（避免跨表事务不一致）
        results[table] = {"inserted": inserted, "skipped": skipped}

    return results


def _table_columns(conn, table: str) -> Set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _select_target_user_ids(conn) -> List[str]:
    """按最近活动优先返回目标库中已有的 user_id。"""
    columns = _table_columns(conn, "sessions")
    if not columns or "user_id" not in columns:
        return []

    order_candidates = [
        col for col in ("last_activity_at", "updated_at", "created_at")
        if col in columns
    ]
    if order_candidates:
        coalesced = ", ".join(order_candidates)
        query = (
            "SELECT user_id FROM sessions "
            "WHERE user_id IS NOT NULL "
            "GROUP BY user_id "
            f"ORDER BY MAX(COALESCE({coalesced})) DESC"
        )
    else:
        query = (
            "SELECT user_id FROM sessions "
            "WHERE user_id IS NOT NULL "
            "GROUP BY user_id"
        )
    return [row[0] for row in conn.execute(query).fetchall() if row[0]]


def _preview_path_mapping(db_source: Path, ctx: RestoreContext):
    """预览跨平台路径映射影响。"""
    if not (ctx.needs_remap and ctx.auto_map and ctx.source_prefix):
        return

    try:
        src_conn = sqlite3.connect(str(db_source))
        slash_pattern = ctx.source_prefix.replace("\\", "/") + "/%"
        backslash_pattern = ctx.source_prefix.replace("/", "\\") + "\\%"
        rows = src_conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE cwd LIKE ? OR cwd LIKE ?",
            (slash_pattern, backslash_pattern),
        ).fetchone()
        sessions = rows[0] if rows else 0
        src_conn.close()
    except sqlite3.Error:
        sessions = 0

    if sessions > 0:
        print(f"\n  [跨平台] 检测到 {sessions} 条 session 需要路径映射")
        print(f"    源前缀: {ctx.source_prefix}")
        print(f"    目标前缀: {ctx.target_prefix}")


def _preview_user_id_mapping(db_source: Path, ctx: RestoreContext, detect_target: bool):
    """预览账号 user_id 合并影响。"""
    if not ctx.auto_map:
        return

    source_conn = sqlite3.connect(str(db_source))
    try:
        ctx.detect_user_ids(source_conn, detect_target=detect_target)
    finally:
        source_conn.close()

    source_ids = [uid for uid in ctx._source_user_ids if uid]
    if not source_ids:
        return
    if ctx._target_user_id:
        to_merge = [uid for uid in source_ids if uid != ctx._target_user_id]
        if to_merge:
            print(f"\n  [账号] 将 {len(to_merge)} 个源 user_id 合并到目标账号 {ctx._target_user_id}")
    else:
        print("\n  [账号] 未检测到目标账号 user_id")
        print("  [提示] 新账号下先创建一次会话，或使用 --target-user-id <uuid> 后再恢复")


def _apply_database_auto_map(conn, ctx: RestoreContext, allow_user_merge: bool):
    """对已导入目标库的数据执行跨平台路径和 user_id 映射。"""
    if ctx.needs_remap:
        session_count = _remap_session_paths(conn, ctx.source_prefix, ctx.target_prefix)
        ctx.sessions_remapped += session_count
        if session_count:
            print(f"\n  [跨平台] session cwd 映射: {session_count} 条")

        ws_updated = _remap_workspace_paths(conn, ctx.source_prefix, ctx.target_prefix)
        if ws_updated:
            print(f"  [跨平台] workspaces 映射: {ws_updated} 条")

    if allow_user_merge:
        _merge_user_ids(conn, ctx)


def _merge_user_ids(conn, ctx: RestoreContext) -> int:
    """将源账号会话合并到目标账号 user_id。"""
    source_ids = [uid for uid in ctx._source_user_ids if uid]
    if not source_ids:
        return 0
    if not ctx._target_user_id:
        print("  [账号] 未检测到目标账号 user_id，跳过 user_id 合并")
        print("  [提示] 新账号下先创建一次会话，或使用 --target-user-id <uuid> 后再恢复")
        return 0

    merged_total = 0
    for source_id in source_ids:
        if source_id == ctx._target_user_id:
            continue
        merged_total += conn.execute(
            "UPDATE sessions SET user_id=? WHERE user_id=?",
            (ctx._target_user_id, source_id)
        ).rowcount

    ctx.user_id_merged += merged_total
    if merged_total:
        label = "指定目标账号" if ctx.target_user_id_explicit else "目标账号"
        print(f"  [账号] user_id 合并到{label}: {merged_total} 条")
    return merged_total


def _remap_session_paths(conn, source_prefix: str, target_prefix: str) -> int:
    """更新 sessions 表的 cwd 字段进行跨平台路径映射"""
    if not source_prefix or not target_prefix:
        return 0

    # 查找所有匹配源前缀的 session
    src_pattern = source_prefix.replace("\\", "/") + "/%"
    rows = conn.execute(
        "SELECT id, cwd FROM sessions WHERE cwd LIKE ?",
        (src_pattern,)
    ).fetchall()

    # 也匹配反斜杠版本
    src_pattern_bs = source_prefix.replace("/", "\\") + "\\%"
    rows += conn.execute(
        "SELECT id, cwd FROM sessions WHERE cwd LIKE ?",
        (src_pattern_bs,)
    ).fetchall()

    # 去重
    seen = set()
    unique_rows = []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            unique_rows.append(r)

    updated = 0
    for sid, old_cwd in unique_rows:
        if not old_cwd:
            continue
        # 尝试两种分隔符格式
        for sep in ("/", "\\"):
            src = source_prefix.replace("\\", sep).replace("/", sep)
            if old_cwd.startswith(src):
                relative = old_cwd[len(src):].lstrip("/\\")
                new_cwd = join_mapped_path(target_prefix, relative)
                conn.execute("UPDATE sessions SET cwd=? WHERE id=?", (new_cwd, sid))
                updated += 1
                break

    return updated


def _remap_workspace_paths(conn, source_prefix: str, target_prefix: str) -> int:
    """更新 workspaces 表的 path 字段"""
    if not source_prefix or not target_prefix:
        return 0

    rows = conn.execute("SELECT path FROM workspaces").fetchall()
    updated = 0
    for (path,) in rows:
        if not path:
            continue
        for sep in ("/", "\\"):
            src = source_prefix.replace("\\", sep).replace("/", sep)
            if path.startswith(src):
                relative = path[len(src):].lstrip("/\\")
                new_path = join_mapped_path(target_prefix, relative)
                conn.execute(
                    "UPDATE workspaces SET path=? WHERE path=?",
                    (new_path, path)
                )
                updated += 1
                break
    return updated


def _remap_project_dirs(ctx: RestoreContext):
    """重命名 projects 目录以匹配新的 cwd 路径"""
    projects_dir = ctx.wb_home / "projects"
    if not projects_dir.exists():
        print("  [跨平台] projects 目录不存在，无需重命名")
        return

    if not ctx.source_prefix or not ctx.target_prefix:
        print("  [跨平台] 缺少路径前缀，无法重命名 projects")
        return

    source_dir_prefix = cwd_to_project_dir(ctx.source_prefix)
    target_dir_prefix = cwd_to_project_dir(ctx.target_prefix)
    if not source_dir_prefix or not target_dir_prefix:
        print("  [跨平台] 无法编码路径前缀，跳过 projects 重命名")
        return
    if source_dir_prefix == target_dir_prefix:
        print("  [跨平台] projects 目录无需重命名")
        return

    for entry in list(projects_dir.iterdir()):
        if not entry.is_dir():
            continue
        old_name = entry.name
        if not old_name.startswith(source_dir_prefix):
            continue

        new_name = target_dir_prefix + old_name[len(source_dir_prefix):]
        if new_name == old_name:
            continue

        old_path = projects_dir / old_name
        new_path = projects_dir / new_name
        if new_path.exists():
            print(f"  [跨平台] 合并: {old_name} → {new_name}")
            _merge_tree_move(old_path, new_path)
        else:
            print(f"  [跨平台] 重命名: {old_name} → {new_name}")
            shutil.move(str(old_path), str(new_path))
        ctx.project_dirs_renamed += 1

    if ctx.project_dirs_renamed:
        print(f"  [跨平台] projects 目录重命名: {ctx.project_dirs_renamed} 个")
    else:
        print(f"  [跨平台] projects 目录无需重命名")


def _merge_tree_move(source: Path, target: Path):
    """将 source 合并进 target，已存在的目标文件保持不变。"""
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        rel = item.relative_to(source)
        dest = target / rel
        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif item.is_file() and not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(dest))
    shutil.rmtree(str(source))


def _ensure_workspace_dirs(ctx: RestoreContext):
    """为所有 session 创建缺失的工作区目录并注册到 workspaces 表。

    跨平台迁移后，session 的 cwd 指向新路径，但实际目录可能不存在。
    此函数自动创建目录并恢复基本结构。
    """
    conn = sqlite3.connect(str(ctx.wb_home / "workbuddy.db"))
    try:
        session_cols = _table_columns(conn, "sessions")
        if "cwd" not in session_cols:
            return
        if "deleted_at" in session_cols:
            query = "SELECT cwd FROM sessions WHERE deleted_at IS NULL AND cwd IS NOT NULL"
        else:
            query = "SELECT cwd FROM sessions WHERE cwd IS NOT NULL"
        sessions = conn.execute(query).fetchall()
    finally:
        conn.close()

    created = 0
    for (cwd,) in sessions:
        cwd_path = Path(cwd)
        if cwd_path.exists():
            continue

        # 创建工作区目录和 .workbuddy/memory/
        memory_dir = cwd_path / ".workbuddy" / "memory"
        try:
            memory_dir.mkdir(parents=True, exist_ok=True)
            created += 1
        except (OSError, PermissionError):
            continue

    if created:
        print(f"  [跨平台] 创建工作区目录: {created} 个")

    # 注册到 workspaces 表。不同版本 schema 可能不同，按可用字段写入。
    conn = sqlite3.connect(str(ctx.wb_home / "workbuddy.db"))
    try:
        workspace_cols = _table_columns(conn, "workspaces")
        if "path" not in workspace_cols:
            return

        registered = 0
        now = int(datetime.datetime.now().timestamp() * 1000)
        for (cwd,) in sessions:
            exists = conn.execute(
                "SELECT 1 FROM workspaces WHERE path=?", (cwd,)
            ).fetchone()
            if not exists:
                if "last_opened_at" in workspace_cols:
                    conn.execute(
                        "INSERT INTO workspaces (path, last_opened_at) VALUES (?, ?)",
                        (cwd, now)
                    )
                else:
                    conn.execute("INSERT INTO workspaces (path) VALUES (?)", (cwd,))
                registered += 1
        conn.commit()
    finally:
        conn.close()

    if registered:
        print(f"  [跨平台] 注册工作区到 workspaces 表: {registered} 个")


# ============================================================
# 沙箱权限预检测
# ============================================================

def check_write_permissions(required_paths: List[Path], label: str = "操作") -> bool:
    """
    检测关键目录/文件的可写性。

    在执行备份或恢复前调用，提前发现沙箱权限问题，
    避免操作到一半因权限不足而失败导致数据不一致。

    Args:
        required_paths: 需要可写的路径列表
        label: 操作名称（用于提示信息，如"恢复"或"备份"）

    Returns:
        True = 全部可写，False = 存在不可写的路径
    """
    failed = []
    for p in required_paths:
        if p.is_dir():
            # 尝试在目录下创建临时文件来测试可写性
            try:
                test_file = p / ".wb_migrate_permission_test"
                test_file.write_text("test", encoding="utf-8")
                test_file.unlink(missing_ok=True)
            except (PermissionError, OSError) as e:
                failed.append((str(p), str(e)))
        elif p.is_file():
            # 对已存在的文件，尝试以追加模式打开来测试可写性
            try:
                with open(str(p), "ab") as f:
                    pass
            except (PermissionError, OSError) as e:
                failed.append((str(p), str(e)))
        # 不存在的路径：检查其父目录是否可写
        elif not p.exists():
            parent = p.parent
            if parent.exists():
                try:
                    test_file = parent / ".wb_migrate_permission_test"
                    test_file.write_text("test", encoding="utf-8")
                    test_file.unlink(missing_ok=True)
                except (PermissionError, OSError) as e:
                    failed.append((str(p), f"父目录不可写: {e}"))

    if failed:
        print(f"\n{'!'*60}")
        print(f"  [权限检测失败] 以下路径不可写，{label}操作无法继续：")
        print(f"{'!'*60}")
        for path, err in failed:
            print(f"    ✗ {path}")
            print(f"      原因: {err}")
        print()
        print(f"  解决方法：")
        print(f"  1. 在 WorkBuddy Desktop 中，点击命令确认框的「允许」即可执行")
        print(f"     （安全沙箱模式下每条命令只需确认一次）")
        print(f"  2. 或切换底部按钮为「完全放开-无需确认」模式")
        print(f"  3. 或在系统终端中手动运行此脚本：")
        print(f"     python3 wb_migrate.py restore <备份文件>")
        print(f"{'!'*60}\n")
        return False

    print(f"  [权限检测] 全部 {len(required_paths)} 个路径可写 ✓")
    return True


# ============================================================
# 恢复后校验引擎
# ============================================================

def validate_restore(ctx: RestoreContext) -> Dict:
    """恢复完成后校验数据完整性"""
    print(f"  --- 校验 ---")
    issues = []
    db_path = ctx.wb_home / "workbuddy.db"

    if not db_path.exists():
        issues.append("数据库文件不存在")
        return {"passed": False, "issues": issues}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 1. 校验 sessions 和 cwd
    sessions = conn.execute("SELECT id, cwd, user_id FROM sessions").fetchall()
    print(f"  sessions: {len(sessions)} 条")

    missing_cwd = 0
    missing_workspace = 0
    for s in sessions:
        cwd = s["cwd"]
        if not cwd:
            missing_cwd += 1
            continue
        # 检查 workspace 目录是否存在
        if not Path(cwd).exists():
            missing_workspace += 1

    if missing_cwd:
        issues.append(f"{missing_cwd} 条 session 缺少 cwd")
    if missing_workspace:
        issues.append(f"{missing_workspace} 条 session 的 workspace 目录不存在")

    # 2. 校验 projects 目录
    projects_dir = ctx.wb_home / "projects"
    if projects_dir.exists():
        project_dirs = [d for d in projects_dir.iterdir() if d.is_dir()]
        print(f"  projects: {len(project_dirs)} 个目录")

        # 检查每个 session 是否有对应的 project 目录
        missing_projects = 0
        for s in sessions:
            cwd = s["cwd"]
            if not cwd:
                continue
            expected = cwd_to_project_dir(cwd)
            proj_path = projects_dir / expected
            if not proj_path.exists():
                missing_projects += 1

        if missing_projects:
            issues.append(f"{missing_projects} 条 session 缺少 project 目录")
    else:
        issues.append("projects 目录不存在")

    # 3. 校验 tasks 目录
    tasks_dir = ctx.wb_home / "tasks"
    if tasks_dir.exists():
        task_dirs = [d for d in tasks_dir.iterdir() if d.is_dir()]
        print(f"  tasks: {len(task_dirs)} 个任务组")
        session_ids = set(s["id"] for s in sessions)
        orphan_tasks = [d.name for d in task_dirs if d.name not in session_ids]
        if orphan_tasks:
            issues.append(f"{len(orphan_tasks)} 个任务组无对应 session")

    # 4. 校验 skills
    skills_dir = ctx.wb_home / "skills"
    if skills_dir.exists():
        skill_count = sum(1 for _ in skills_dir.iterdir() if _.is_dir())
        print(f"  skills: {skill_count} 个")

    # 5. 校验 user_id 一致性
    user_ids = conn.execute(
        "SELECT DISTINCT user_id FROM sessions WHERE user_id IS NOT NULL"
    ).fetchall()
    if len(user_ids) > 1:
        issues.append(f"存在 {len(user_ids)} 个不同的 user_id: {[u['user_id'] for u in user_ids]}")

    conn.close()

    passed = len(issues) == 0
    status = "[OK]" if passed else "[WARN]"
    if passed:
        print(f"  {status} 所有校验通过")
    else:
        print(f"  {status} 发现 {len(issues)} 个问题")

    return {"passed": passed, "issues": issues}


# ============================================================
# 辅助函数
# ============================================================

def _analyze_db_diff(db_source: Path, db_target: Path) -> Dict:
    """分析两个数据库的差异"""
    source_conn = sqlite3.connect(str(db_source))
    target_conn = sqlite3.connect(str(db_target))
    results = {}
    for table, pk_col in DB_TABLES:
        if table == "migration_meta":
            continue
        try:
            src_count = source_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            tgt_count = target_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            src_ids = set(r[0] for r in source_conn.execute(f"SELECT {pk_col} FROM {table}"))
            tgt_ids = set(r[0] for r in target_conn.execute(f"SELECT {pk_col} FROM {table}"))
            results[table] = {
                "new": len(src_ids - tgt_ids),
                "conflict": len(src_ids & tgt_ids),
                "src_total": src_count,
                "tgt_total": tgt_count,
            }
        except sqlite3.OperationalError:
            results[table] = {"new": 0, "conflict": 0, "src_total": 0, "tgt_total": 0}
    source_conn.close()
    target_conn.close()
    return results


def _restore_dir(src: Path, target: Path, force: bool = False):
    """恢复目录：智能合并"""
    if not target.exists():
        shutil.copytree(src, target)
        print(f"  [OK] {src.name}/ (新建)")
        return

    stats = {"added": 0, "updated": 0, "skipped": 0}
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        dest = target / rel
        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            if dest.exists():
                if force:
                    shutil.copy2(item, dest)
                    stats["updated"] += 1
                else:
                    stats["skipped"] += 1
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
                stats["added"] += 1

    print(f"  [OK] {src.name}/ (+{stats['added']}, "
          f"更新 {stats['updated']}, 跳过 {stats['skipped']})")


def _restore_file(src: Path, target: Path, force: bool = False):
    """恢复单个文件"""
    if target.exists() and not force:
        print(f"  [.] {src.name} (已存在, 跳过)")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    print(f"  [OK] {src.name}")


def _dry_list_dir(src: Path, target: Path):
    """预览目录操作"""
    if not target.exists():
        print(f"  [DRY-RUN] {src.name}/ (新建)")
        return
    added = sum(1 for item in src.rglob("*")
                if item.is_file() and not (target / item.relative_to(src)).exists())
    print(f"  [DRY-RUN] {src.name}/ (+{added} 文件)")


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _safe_extract_tar(tar: tarfile.TarFile, target_dir: Path):
    """兼容 Python 3.9 的安全解包，拒绝路径穿越、链接和特殊文件。"""
    root = target_dir.resolve()
    members = tar.getmembers()
    for member in members:
        _validate_tar_member(member, root)
    for member in members:
        tar.extract(member, path=target_dir)


def _validate_tar_member(member: tarfile.TarInfo, root: Path):
    raw_name = member.name
    normalized = raw_name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]

    if not raw_name or normalized.startswith("/") or ".." in parts:
        raise ValueError(f"备份包包含不安全路径: {raw_name}")
    if PureWindowsPath(raw_name).is_absolute():
        raise ValueError(f"备份包包含 Windows 绝对路径: {raw_name}")
    if member.issym() or member.islnk():
        raise ValueError(f"备份包包含链接文件: {raw_name}")
    if member.ischr() or member.isblk() or member.isfifo():
        raise ValueError(f"备份包包含特殊文件: {raw_name}")

    target = (root / raw_name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"备份包路径越界: {raw_name}") from exc


def _verify_archive(archive_path: str) -> bool:
    """验证备份包完整性"""
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getmembers()
            has_meta = any(m.name == "metadata.json" for m in members)
            if not has_meta:
                print("  [WARN] 备份包缺少 metadata.json")
                return False
            verify_root = (Path(tempfile.gettempdir()) / "wb_migrate_verify").resolve()
            for m in members:
                try:
                    _validate_tar_member(m, verify_root)
                except ValueError as e:
                    print(f"  [WARN] {e}")
                    return False
            bad_files = []
            for m in members:
                try:
                    if m.isfile():
                        f = tar.extractfile(m)
                        if f:
                            f.read()
                except Exception as e:
                    bad_files.append((m.name, str(e)))
            if bad_files:
                print(f"  [WARN] {len(bad_files)} 个文件损坏")
                return False
        return True
    except Exception as e:
        print(f"  [ERROR] 备份包无效: {e}")
        return False


# ============================================================
# 扫描模式
# ============================================================

def scan_wb_data() -> Dict:
    """扫描当前 WorkBuddy 数据"""
    wb_home = get_wb_home()

    print(f"\n{'='*60}")
    print(f"  WorkBuddy 数据扫描")
    print(f"  平台: {platform.system()} {platform.release()}")
    print(f"  目录: {wb_home}")
    print(f"{'='*60}\n")

    result = {}

    # 数据库
    db_path = wb_home / "workbuddy.db"
    if db_path.exists():
        db_size = db_path.stat().st_size
        conn = sqlite3.connect(str(db_path))
        sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE deleted_at IS NULL"
        ).fetchone()[0]
        automations = conn.execute(
            "SELECT COUNT(*) FROM automations WHERE deleted_at IS NULL"
        ).fetchone()[0]
        workspaces = conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
        conn.close()
        print(f"  workbuddy.db ({_fmt_size(db_size)})")
        print(f"    Sessions:     {sessions}")
        print(f"    Automations:  {automations}")
        print(f"    Workspaces:   {workspaces}")
        result["database"] = {
            "sessions": sessions, "automations": automations,
            "workspaces": workspaces, "size": db_size
        }

    # 目录统计
    for key, item in BACKUP_MANIFEST.items():
        if item["type"] == "dir":
            dir_path = wb_home / item["path"]
            if dir_path.exists() and dir_path.is_dir():
                count, total_size = _count_dir(dir_path)
                print(f"  {item['path']}/  ({_fmt_size(total_size)}, {count} 文件)")
                result[key] = {"count": count, "size": total_size}
        elif item["type"] == "files":
            found = sum(1 for fn in item.get("paths", [])
                       if (wb_home / fn).exists())
            if found:
                print(f"  配置文件: {found}/{len(item['paths'])} 个")
                result[key] = {"found": found, "total": len(item['paths'])}

    # 大目录
    print(f"\n  较大目录 (不在标准备份中):")
    for d in SKIP_DIRS_DEFAULT:
        dp = wb_home / d
        try:
            if dp.exists():
                count, total_size = _count_dir(dp)
                print(f"    {d}/  ({_fmt_size(total_size)}, {count} 文件)")
        except (PermissionError, OSError):
            print(f"    {d}/  (无权限)")

    print(f"{'='*60}\n")
    return result


def _count_dir(path: Path) -> Tuple[int, int]:
    count, total_size = 0, 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                count += 1
                try:
                    total_size += item.stat().st_size
                except OSError:
                    pass
    except PermissionError:
        pass
    return count, total_size


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=f"wb-migrate v{VERSION} — WorkBuddy 数据迁移工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  wb-migrate scan                              # 扫描当前数据
  wb-migrate backup --output ~/Desktop         # 备份到桌面
  wb-migrate info wb-backup-*.tar.gz           # 查看备份信息
  wb-migrate restore backup.tar.gz             # 恢复 (智能合并)
  wb-migrate restore backup.tar.gz --dry-run   # 预览
  wb-migrate restore backup.tar.gz --force     # 强制覆盖
  wb-migrate restore backup.tar.gz --auto-map  # 自动映射路径和账号 user_id
  wb-migrate restore backup.tar.gz --auto-map --target-user-id <uuid>
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="操作命令")

    # scan
    subparsers.add_parser("scan", help="扫描当前 WorkBuddy 数据")

    # backup
    bp = subparsers.add_parser("backup", help="创建备份")
    bp.add_argument("--output", "-o", default=".",
                    help="输出目录 (默认: 当前目录)")
    bp.add_argument("--include-binaries", action="store_true",
                    help="包含 binaries/app/plugins")

    # info
    ip = subparsers.add_parser("info", help="查看备份信息")
    ip.add_argument("archive", help="备份文件路径")

    # restore
    rp = subparsers.add_parser("restore", help="恢复备份")
    rp.add_argument("archive", help="备份文件路径")
    rp.add_argument("--dry-run", action="store_true", help="预览模式")
    rp.add_argument("--force", action="store_true", help="强制覆盖")
    rp.add_argument("--auto-map", action="store_true",
                    help="自动路径映射和账号 user_id 合并")
    rp.add_argument("--target-user-id", default=None,
                    help="指定恢复后 sessions 使用的目标账号 user_id")

    # migrate (同 restore)
    mp = subparsers.add_parser("migrate", help="迁移 (同 restore)")
    mp.add_argument("archive", help="备份文件路径")
    mp.add_argument("--dry-run", action="store_true")
    mp.add_argument("--force", action="store_true")
    mp.add_argument("--auto-map", action="store_true")
    mp.add_argument("--target-user-id", default=None)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "scan":
            scan_wb_data()

        elif args.command == "backup":
            archive_path = create_backup(
                output_dir=args.output,
                include_binaries=args.include_binaries)
            print(f"备份文件: {archive_path}")

        elif args.command == "info":
            show_backup_info(args.archive)

        elif args.command in ("restore", "migrate"):
            print("  验证备份包...")
            if _verify_archive(args.archive):
                print("  [OK] 验证通过\n")
                restore_backup(
                    archive_path=args.archive,
                    dry_run=args.dry_run,
                    force=args.force,
                    auto_map=getattr(args, "auto_map", False),
                    target_user_id=getattr(args, "target_user_id", None),
                )
            else:
                print("\n  [ERROR] 备份验证失败，终止")
                sys.exit(1)

    except FileNotFoundError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
