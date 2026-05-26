# wb-migrate — WorkBuddy 全量数据迁移工具 v2.0.3

> 跨平台 (macOS / Windows / Linux) 的备份、恢复、迁移工具  
> 支持智能合并、跨平台路径映射、跨账号 user_id 合并、工作区目录自动创建、恢复后自动校验

---

## 目录

- [快速开始](#快速开始)
- [核心概念](#核心概念)
- [架构设计](#架构设计)
- [备份包结构](#备份包结构)
- [智能合并策略](#智能合并策略)
- [跨平台路径与账号映射](#跨平台路径与账号映射)
- [恢复后自动校验](#恢复后自动校验)
- [API 参考](#api-参考)
- [场景指南](#场景指南)
- [数据隔离说明](#数据隔离说明)
- [安全考量](#安全考量)
- [FAQ](#faq)
- [维护信息](#维护信息)

---

## 快速开始

### 安装

wb-migrate 是一个自定义 WorkBuddy skill，不是 WorkBuddy 默认内置功能。安装方式有两种：

1. 手动安装：将整个 `wb-migrate/` 目录复制到 `~/.workbuddy/skills/wb-migrate/`
2. 发布安装：提交给 WorkBuddy / Skill Marketplace，由 WorkBuddy 侧安装或分发

安装完成后，核心脚本位于：

```bash
~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py
```

### 基本用法

```bash
# 1. 扫描当前数据概览
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py scan

# 2. 创建备份
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py backup --output ~/Desktop

# 3. 查看备份内容
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py info ~/Desktop/wb-backup-*.tar.gz

# 4. 恢复备份 (智能合并，不覆盖已有数据)
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore ~/Desktop/wb-backup-*.tar.gz

# 5. 预览恢复 (安全，不实际修改)
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore ~/Desktop/wb-backup-*.tar.gz --dry-run

# 6. 跨账号/跨平台恢复 (自动映射路径 + user_id)
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore ~/Desktop/wb-backup-*.tar.gz --auto-map

# 如果目标账号 user_id 无法自动检测，可显式指定
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore ~/Desktop/wb-backup-*.tar.gz --auto-map --target-user-id <uuid>
```

---

## 核心概念

### WorkBuddy 数据模型

WorkBuddy 的数据分布在两个关键存储层：

| 存储层 | 位置 | 内容 | 关键字段 |
|--------|------|------|----------|
| **SQLite 数据库** | `~/.workbuddy/workbuddy.db` | Sessions, Automations, Workspaces | `user_id` (账号隔离) |
| **文件系统** | `~/.workbuddy/` | Skills, Projects, Tasks, Connectors | `cwd` (工作目录) |

### 关键关系链

```
Session (sessions 表)
  ├── id → tasks/<session-id>/          (任务列表)
  ├── cwd → projects/<encoded>/<sid>/   (对话历史)
  ├── user_id                            (账号隔离)
  └── workspace → workspaces 表          (工作区元数据)
```

**路径编码规则** (`cwd` → project 目录名):

| 平台 | cwd 示例 | project 目录名 |
|------|---------|---------------|
| Windows | `C:\Users\Alice\WorkBuddy\xxx` | `c-Users-Alice-WorkBuddy-xxx` |
| macOS | `/Users/alice/WorkBuddy/xxx` | `Users-alice-WorkBuddy-xxx` |
| Linux | `/home/user/WorkBuddy/xxx` | `home-user-WorkBuddy-xxx` |

### user_id 隔离机制

- 每个 WorkBuddy 账号有唯一的 `user_id` (UUID)
- `sessions.user_id` 决定会话归属
- 客户端的 UI 只显示当前登录 `user_id` 的会话
- 使用 `restore --auto-map` 时，工具会把源账号 `user_id` 合并到目标账号，换账号恢复后会话可见
- **Skills / Projects / Connectors 不区分 user_id** — 所有账号共享

---

## 架构设计

```
wb-migrate/
├── SKILL.md                    # Skill 定义 (触发场景、使用方法)
├── README.md                   # 本文档
├── agents/
│   └── openai.yaml             # 可选 UI 元数据，便于支持该格式的客户端展示 skill
├── scripts/
│   └── wb_migrate.py           # 核心引擎
├── wb-migrate.sh               # Linux/macOS 快捷入口
└── wb-migrate.bat              # Windows 快捷入口
```

### 引擎模块

```
┌─────────────────────────────────────────────────────┐
│                     CLI 入口 (main)                   │
│  scan | backup | info | restore | migrate           │
├─────────────────────────────────────────────────────┤
│                                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │  备份引擎      │  │  恢复引擎     │  │  校验引擎   │ │
│  │  create_backup │  │  restore_    │  │  validate_ │ │
│  │               │  │  backup      │  │  restore   │ │
│  └──────┬────────┘  └──────┬───────┘  └─────┬──────┘ │
│         │                  │                 │        │
│  ┌──────┴──────────────────┴─────────────────┴──────┐ │
│  │              跨平台路径映射引擎                      │ │
│  │  detect_source_home_prefix │ remap_path           │ │
│  │  remap_session_paths       │ remap_project_dirs   │ │
│  └──────────────────────────────────────────────────┘ │
│                                                       │
│  ┌──────────────────────────────────────────────────┐ │
│  │              数据库合并引擎                         │ │
│  │  _merge_databases │ _analyze_db_diff              │ │
│  └──────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

### 数据流

```
备份: ~/.workbuddy/ → tar.gz (metadata.json + workbuddy.db + dirs/)
恢复: tar.gz → 解压到临时目录 → 合并数据库 → 复制目录 → 校验
跨平台/跨账号: 检测源/目标OS → 映射 cwd → 重命名 projects → 创建工作区目录 → 合并 user_id
```

---

## 备份包结构

```
wb-backup-{user}-{host}-{timestamp}.tar.gz
├── metadata.json               # 备份元数据 (版本/时间/平台/用户)
├── workbuddy.db                # SQLite 数据库 (全量)
├── skills/                     # 用户 Skills
├── projects/                   # 项目对话数据
├── connectors/                 # MCP 连接器配置
├── sessions/                   # 活跃会话状态
├── tasks/                      # 任务列表
├── teams/                      # 团队配置
├── traces/                     # 执行追踪日志
├── blobs/                      # 二进制对象
├── file-history/               # 文件版本历史
├── artifact-index/             # Artifact 索引
├── media-index/                # 媒体索引
├── shell-snapshots/            # Shell 快照
├── clipboard-images/           # 剪贴板图片
├── local_storage/              # 本地存储键值
├── SOUL.md / IDENTITY.md /     # 身份配置文件
│   USER.md / MEMORY.md
└── mcp.json / models.json /    # 连接器/模型配置
    argv.json
```

### metadata.json 格式

```json
{
  "version": "2.0.3",
  "tool": "wb-migrate",
  "timestamp": "2026-05-26T23:00:00",
  "timestamp_utc": "2026-05-26T15:00:00+00:00",
  "platform": {
    "os": "Darwin",
    "os_release": "24.4.0",
    "machine": "arm64",
    "username": "alice",
    "hostname": "MacBook-Pro",
    "home": "/Users/alice"
  },
  "backup_type": "standard",
  "source_path": "/Users/alice/.workbuddy"
}
```

---

## 智能合并策略

恢复时，wb-migrate 采用 **增量合并**策略，确保已有数据不被覆盖：

### 数据库合并

```
INSERT OR IGNORE — 仅导入目标数据库中不存在的记录
```

| 表 | 主键 | 合并行为 |
|----|------|---------|
| `sessions` | `id` (UUID) | 新 session 插入，已有 session 跳过 |
| `automations` | `id` (UUID) | 同上 |
| `automation_runs` | `thread_id` | 同上 |
| `automation_runtime_state` | `automation_id` | 同上 |
| `workspaces` | `path` | 新 workspace 插入，已有跳过 |
| `migration_meta` | `key` | 同上 |

### 文件合并

```python
if target.exists():
    if force:
        overwrite  # 强制覆盖
    else:
        skip       # 保留已有
else:
    copy           # 新增
```

### 安全性

- 恢复前**自动备份目标数据库** → `workbuddy.db.backup-{timestamp}`
- dry-run 模式可**预览所有操作**（不实际修改）
- 备份使用 SQLite backup API 创建一致性快照，避免遗漏 WAL 数据
- 备份包恢复前必须**通过完整性校验**并安全解包（兼容 Python 3.9+）

---

## 跨平台路径与账号映射

### 为什么需要

WorkBuddy 的核心数据模型中，以下路径是**平台相关**的：

- `sessions.cwd` — 每个会话的工作目录
- `workspaces.path` — 工作区元数据路径
- `projects/<encoded-cwd>/` — 对话历史目录名

如果直接恢复 macOS 备份到 Windows，所有路径仍指向 `/Users/alice/...`，导致：
- 界面提示「工作目录已被重命名或删除」
- 对话历史无法加载
- 任务列表不可用

另外，WorkBuddy 用 `sessions.user_id` 隔离账号。直接恢复旧账号备份时，会话可能仍属于源账号，当前登录账号看不到。

### 映射算法

```
1. 检测源 OS → 从 metadata.json 读取 platform.os
2. 检测目标 OS → 当前 platform.system()
3. 如果源/目标平台不同 → 启用路径映射:
   a. 提取源 home 前缀: /Users/alice (macOS)
   b. 获取目标 home 前缀: C:\Users\Alice (Windows)
   c. 遍历 sessions 表，替换 cwd 前缀
   d. 更新 workspaces 表
   e. 重命名 projects 目录
4. 如果使用 --auto-map → 启用账号合并:
   a. 读取源备份中的 sessions.user_id
   b. 从目标数据库最近会话推断当前账号 user_id
   c. 将源 user_id 更新为目标账号 user_id
   d. 如果目标账号还没有任何会话，可用 --target-user-id 显式指定
```

> v2.0.3: `--auto-map` 同时处理跨平台路径映射、跨账号 user_id 合并、projects 目录重命名，并在跨平台恢复后自动创建缺失的工作区目录和基础 `.workbuddy/memory/` 结构。

### 支持的映射

| 源平台 | 目标平台 | 映射示例 |
|--------|---------|---------|
| macOS → Windows | `/Users/alice/WorkBuddy/x` → `C:\Users\Alice\WorkBuddy\x` |
| Windows → macOS | `C:\Users\Alice\WorkBuddy\x` → `/Users/alice/WorkBuddy/x` |
| macOS → Linux | `/Users/alice/WorkBuddy/x` → `/home/alice/WorkBuddy/x` |
| Linux → macOS | `/home/user/x` → `/Users/user/x` |

### 使用方式

```bash
# 自动检测并映射 (推荐)
python3 wb_migrate.py restore backup.tar.gz --auto-map

# 先预览不执行
python3 wb_migrate.py restore backup.tar.gz --auto-map --dry-run

# 目标账号 user_id 无法自动检测时显式指定
python3 wb_migrate.py restore backup.tar.gz --auto-map --target-user-id <uuid>
```

---

## 恢复后自动校验

恢复完成后，自动执行完整性校验：

```
--- 校验 ---
sessions: 40 条
projects: 33 个目录
tasks: 7 个任务组
skills: 50 个
[OK] 所有校验通过
```

### 校验项

| 检查项 | 说明 |
|--------|------|
| sessions-cwd 有效性 | 所有 session 都有 cwd |
| workspace 存在性 | cwd 指向的目录存在 |
| projects 目录匹配 | 每个 session 有对应 project 目录 |
| tasks-sessions 关联 | task 目录能找到对应 session |
| user_id 一致性 | 所有 session 同属一个 user_id |

---

## API 参考

### 命令

| 命令 | 说明 | 参数 |
|------|------|------|
| `scan` | 扫描当前数据 | — |
| `backup` | 创建备份 | `--output DIR` `--include-binaries` |
| `info <file>` | 查看备份信息 | `archive` 文件路径 |
| `restore <file>` | 恢复备份 | `--dry-run` `--force` `--auto-map` `--target-user-id` |
| `migrate <file>` | 迁移 (同 restore) | 同上 |

### Python API

```python
from wb_migrate import (
    create_backup,      # → str (备份文件路径)
    restore_backup,     # → Dict (恢复结果)
    show_backup_info,   # → Dict (元数据)
    scan_wb_data,       # → Dict (扫描结果)
    validate_restore,   # → Dict (校验结果)
)
```

---

## 场景指南

### 场景 1: 同设备换账号

```bash
# 1. 旧账号下备份
wb-migrate backup --output ~/Desktop

# 2. 登录新账号
# 建议先在新账号里创建一次任意会话，让目标数据库有当前账号 user_id

# 3. 恢复 (会把旧账号 session 合并到新账号 user_id)
wb-migrate restore ~/Desktop/wb-backup-*.tar.gz --auto-map

# 如果新账号还没有任何会话，无法自动检测目标 user_id 时：
wb-migrate restore ~/Desktop/wb-backup-*.tar.gz --auto-map --target-user-id <uuid>
```

### 场景 2: 换电脑 (macOS → Windows)

```bash
# 1. macOS 上备份
wb-migrate backup --output ~/Desktop

# 2. 传输到 Windows (U盘/网盘/局域网)

# 3. Windows 上恢复
wb-migrate restore D:\backup.tar.gz --auto-map
```

### 场景 3: 仅迁移 Skills

```bash
# 备份后，手动提取
tar -xzf backup.tar.gz skills/
cp -r skills/* ~/.workbuddy/skills/
```

### 场景 4: 多设备同步 (通过备份)

```bash
# 设备A: 备份 → 分享
wb-migrate backup --output ~/Desktop

# 设备B: 恢复 (--auto-map 处理平台差异)
wb-migrate restore backup.tar.gz --auto-map
```

---

## 数据隔离说明

WorkBuddy 目前的数据隔离机制：

| 数据类型 | 隔离方式 | 跨账号可见 | 加密 |
|----------|---------|-----------|------|
| Sessions (会话) | `user_id` 字段 | 默认不可见；`--auto-map` 合并后可见 | ❌ 明文 |
| Tasks (任务) | 挂在 session ID 下 | ❌ 不可见 | ❌ 明文 |
| Automations (自动化) | `user_id` (通过 session) | ❌ 不可见 | ❌ 明文 |
| Skills | 无隔离 | ✅ 共享 | ❌ 明文 |
| Projects (对话数据) | 无隔离 (按路径组织) | ✅ 共享 | ❌ 明文 |
| Connectors (MCP) | 无隔离 | ✅ 共享 | ❌ 明文 |

> **注意**: 所有数据以明文存储在 SQLite + JSON 文件中。如有安全需求，建议在分享备份包前做脱敏处理。

---

## 安全考量

### 恢复前自动备份

每次恢复数据库前，wb-migrate 会自动创建备份：
```
~/.workbuddy/workbuddy.db.backup-20260526-230000
```

如果恢复结果不符合预期，可以手动还原：
```bash
cp ~/.workbuddy/workbuddy.db.backup-* ~/.workbuddy/workbuddy.db
```

### 不会备份的内容

以下目录因体积较大或有权限问题，默认不备份：
- `binaries/` (~540MB, 运行时二进制)
- `app/` (~240MB, 应用程序)
- `plugins/` (~72MB, 插件)
- `skills-marketplace/` (~52MB, 市场缓存)
- `logs/` (受权限保护)

### 备份包传输安全

备份包是标准 tar.gz，可能包含敏感数据（对话历史、配置文件中的 token）。建议：
- 使用加密通道传输（如加密 U 盘、端到端加密的云盘）
- 传输后删除中间文件
- 不将备份包上传到公开位置

---

## FAQ

### Q: 恢复后任务列表能看到但点击报错「工作目录已被删除」？

A: 这是跨平台路径不匹配的问题。使用 `--auto-map` 重新恢复：

```bash
wb-migrate restore backup.tar.gz --auto-map
```

### Q: 为什么 Skills 恢复了但对话历史看不到？

A: 对话历史通过 `sessions.cwd` 关联。检查两点：
1. session 的 `user_id` 是否是当前账号
2. session 的 `cwd` 路径是否在本地存在

使用 `--auto-map` 可自动修复这两个问题。

### Q: 恢复会覆盖我现有的数据吗？

A: 不会。默认使用智能合并策略：
- 数据库: 只插入新记录 (INSERT OR IGNORE)
- 文件: 已有文件跳过，只新增

使用 `--force` 才会覆盖。

### Q: 不同账号的对话会混在一起吗？

A: 默认恢复会保留原始 `user_id`，客户端只显示当前账号的数据，所以旧账号会话可能不可见。

如果目标是“换账号后继续看到旧账号会话”，使用 `--auto-map`。工具会把备份中的源 `user_id` 合并到目标账号 `user_id`。目标账号没有任何会话、无法自动检测 user_id 时，先在新账号创建一次会话，或使用 `--target-user-id <uuid>` 显式指定。

### Q: 支持增量备份吗？

A: 当前版本是全量备份。每次备份创建完整的包，恢复时自动处理合并。

---

## 维护信息

### 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v2.0.3 | 2026-05 | 跨平台恢复后自动创建缺失工作区目录，并按可用 schema 注册 workspaces 表 |

### 文件清单

```
wb-migrate/
├── SKILL.md            ← WorkBuddy skill 定义文件
├── README.md           ← 本文件
├── agents/
│   └── openai.yaml     ← 可选 UI 元数据，不影响迁移脚本运行
├── scripts/
│   └── wb_migrate.py   ← 核心引擎 Python
├── wb-migrate.sh       ← Linux/macOS 入口
└── wb-migrate.bat      ← Windows 入口
```

### 手动安装

```bash
# 将 wb-migrate 目录复制到 skills 目录
cp -r wb-migrate ~/.workbuddy/skills/

# 安装后运行
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py scan
```

---

## 许可证

MIT License — 自由使用、修改、分发。
