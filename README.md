# wb-migrate — WorkBuddy 全局备份与迁移工具 v3.3.0

> 适配国内版 WorkBuddy **5.6 – 5.9**（本机 5.9.0 实测）；支持全量与按模块选择性导出、三种冲突策略、自动快照回滚、敏感信息脱敏、跨平台（macOS / Windows / Linux）迁移、断点续传；兼容读取 v1/v2 旧备份包。
>
> **v3.3 新增**：`experts` 模块补齐 `plugins/cache/experts`（5.9 专家实体的真实存放位置）并新增插件注册表合并；新增 `scripts/wb_auto.py` —— Windows 免手动自动恢复（注册看门狗，退出 WorkBuddy 即自动恢复、结束后自动重开）；新增跨平台启动器 `scripts/wb.py`（三端统一入口）；Windows 编码与不可读文件加固。
>
> **v3.2 新增**：同设备多账号切换（`accounts` / `switch`）——按账号维度隔离存储，切走即切片、切回即还原，支持三种同名配置策略与三种数据库模式，切换前自动备份、失败自动回滚。
>
> 入口：三端统一 `python3 scripts/wb.py <命令>`（自动检查 Python 3.9+、强制 UTF-8、原样透传参数与退出码）。仓库版另附 `wb-migrate.sh` / `wb-migrate.bat` 两个薄封装，**SkillHub 分发包不含它们**（市场不接受 `.bat`）。`compat` 输出环境兼容性报告，`selftest` 在任意平台跑 18 项端到端一致性用例。

---

## 目录

- [快速开始](#快速开始)
- [模块清单](#模块清单)
- [备份包结构](#备份包结构)
- [导出与导入完整步骤](#导出与导入完整步骤)
- [同设备多账号切换](#同设备多账号切换)
- [冲突策略](#冲突策略)
- [回滚机制](#回滚机制)
- [脱敏与安全](#脱敏与安全)
- [环境兼容性](#环境兼容性)
- [跨平台路径与账号映射](#跨平台路径与账号映射)
- [架构设计](#架构设计)
- [API 参考](#api-参考)
- [数据隔离说明](#数据隔离说明)
- [故障排查](#故障排查)
- [部署与安装](#部署与安装)
- [FAQ](#faq)
- [版本历史](#版本历史)

---

## 快速开始

### 安装

wb-migrate 是自定义 WorkBuddy skill。安装方式：

1. 手动安装：将整个 `wb-migrate/` 目录复制到 `~/.workbuddy/skills/wb-migrate/`
2. 发布安装：通过 Skill Marketplace 分发

核心脚本：`~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py`（单文件，仅 Python 标准库，3.9+）

### 基本用法

```bash
PY=~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py

# 全局参数（须放在子命令前）: --wb-home PATH / -v / -q / --version

python3 $PY scan                                    # 扫描数据概览
python3 $PY modules                                 # 列出 12 个模块
python3 $PY backup  -o ~/Desktop                    # 全量备份（默认脱敏）
python3 $PY backup  -o ~/Desktop --modules skills,memory   # 按模块备份
python3 $PY info    <pkg.tar.gz> --verify           # 查看信息并校验 sha256
python3 $PY verify  <pkg.tar.gz>                    # 仅校验
python3 $PY restore <pkg.tar.gz> --dry-run          # 影子恢复预览（零写入）
python3 $PY restore <pkg.tar.gz>                    # 恢复（merge，不覆盖已有）
python3 $PY restore <pkg.tar.gz> --conflict overwrite --auto-map
python3 $PY rollback --list                         # 列出可回滚记录
python3 $PY rollback --latest                       # 撤销最近一次恢复
```

入口统一用 `python3 scripts/wb.py <命令>`，macOS / Windows / Linux 同一条命令（`wb.py` 只做解释器检查、编码兜底与参数透传）。仓库版另附 `wb-migrate.sh` / `wb-migrate.bat` 两个薄封装，功能等价，**SkillHub 分发的技能包不含这两个文件**。

---

## 模块清单

| 模块 id | 名称 | 包含目录/文件（相对 `~/.workbuddy`） | 数据库表 |
|---------|------|--------------------------------------|----------|
| `conversations` | 对话 | `projects/`、`workspace/`、`sessions/` | sessions、session_usage、workspaces、workspace_trust_decisions |
| `tasks` | 任务 | `tasks/`、`plans/`、`todos/` | — |
| `memory` | 记忆 | `memory/`、`memery/`、`storage/user-*/` | — |
| `config` | 配置 | `local_storage/`、`cli-env/`、`project-resources/` + `settings.json`、`argv.json`、`models.json`、`user-state.json`、`workspace-state.json`、`failover.json`、`.policy-cache.json`、`mcp-approvals.json` | migration_meta |
| `connectors` | 连接器 | `connectors/`（凭据脱敏）+ `mcp.json` | — |
| `experts` | 专家 | `experts/` + `expert-history.json` | — |
| `automations` | 定时任务 | `automations/`、`automation-backups/` | automations、automation_runs、automation_runtime_state、automation_delivery_outbox |
| `skills` | 技能 | `skills/`（默认排除 wb-migrate 自身，`--include-self` 放行） | — |
| `artifacts` | 产物 | `blobs/`、`clipboard-images/`、`artifact-index/`、`media-index/`、`file-history/`、`changes-index/`、`changes-detail/`、`file-tree-manifests/`、`shell-snapshots/`（`traces/` 为 large 默认排除） | — |
| `shared` | 分享文件 | `cloudstudio-deploy-history/`（含 shareLink） | — |
| `apps` | 应用 | `app/`、`appearance-resources/`（large，`--include-large`） | buddy_snapshots |
| `assistant` | 助理 | `user-<uuid>-<nanoid>/`（SOUL/IDENTITY/USER/MEMORY/BOOTSTRAP.md + workspace-state.json）、`inspiration/` + 根级五件套 | — |

- 始终排除：`binaries/`(1.6GB)、`plugins/`、`logs/`、`cache/`、`chrome-devtools-profile/`、`skills-marketplace/`、`connectors-marketplace/`、`security/`、`extensions/`、`*-wal`/`*-shm`、`.DS_Store`、`__pycache__`
- large 目录（`--include-large` 纳入）：`traces/`、`app/`、`appearance-resources/`
- 未归类目录：`--include-other` 纳入；`--exclude <glob>` 追加排除（优先级最高）
- 选择性备份时包内仍保留**完整** workbuddy.db 快照，恢复阶段按模块过滤合并的表（`--all-tables` 覆盖）

---

## 备份包结构

```
wb-backup-{user}-{host}-{时间戳}.tar.gz
├── manifest.json               # 权威清单: format_version=3、模块统计、DB 表/行数、脱敏记录、校验摘要
├── checksums.sha256            # database/** 与 modules/** 逐文件 sha256 (sha256sum -c 兼容格式)
├── metadata.json               # v2 兼容字段子集 (旧工具可识别)
├── database/
│   ├── workbuddy.db            # SQLite backup API 一致性快照 (含 WAL 未 checkpoint 数据)
│   └── edge-sync-mapping-v4.db # 第二个 SQLite, 原样快照 (恢复为独立文件, 不合并)
└── modules/
    ├── conversations/  projects/...  workspace/...  sessions/...
    ├── tasks/          tasks/...  plans/...  todos/...
    ├── memory/         memory/...  memery/...  storage/user-*/...
    ├── config/         local_storage/... + settings.json 等 8 个配置文件
    ├── connectors/     connectors/... + mcp.json
    ├── experts/        experts/... + expert-history.json
    ├── automations/    automations/...  automation-backups/...
    ├── skills/         skills/<name>/...
    ├── artifacts/      blobs/...  clipboard-images/...  artifact-index/...
    ├── shared/         cloudstudio-deploy-history/...
    ├── apps/           app/... (仅 --include-large)
    └── assistant/      user-*/...  inspiration/... + 根级五件套
```

### manifest.json 关键字段

```json
{
  "format_version": 3,
  "tool_version": "3.3.0",
  "backup_id": "20261022-115904-0001",
  "platform": {"os": "Darwin", "username": "li", "home": "/Users/li", "path_sep": "/"},
  "source": {"home_prefix": "/Users/li", "user_ids": ["969f…", "de4e…"]},
  "options": {"modules": ["conversations", "…"], "redacted": true},
  "modules": {"conversations": {"files_count": 455, "bytes": 187500000}},
  "database": {"workbuddy.db": {"sha256": "…", "tables": {"sessions": {"rows": 159}}}},
  "redaction": {"enabled": true, "redacted_files": ["mcp.json", "…"], "omitted_files": ["connectors/*/.master.key"]},
  "checksums": {"algorithm": "sha256", "digest": "<checksums.sha256 内容的 sha256>"}
}
```

### 旧包兼容（v1/v2）

- 无 `manifest.json` 的旧包：自动读取 `metadata.json` 合成 legacy manifest，推断源 home 前缀
- 旧包恢复时顶层条目必须命中模块白名单；未知条目（如 `binaries/`）默认跳过并告警，`--allow-unknown` 放行
- 旧包无校验清单，仅做 tar 可读性检查；可能含明文凭据，分享前请检查

---

## 导出与导入完整步骤

### 导出（源设备）

```bash
PY=~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py

# ① 扫描确认数据范围（可选）
python3 $PY scan

# ② 创建备份（全量 + 脱敏；或 --modules 指定模块）
python3 $PY backup -o ~/Desktop
python3 $PY backup -o ~/Desktop --modules conversations,tasks,memory,skills

# ③ 校验备份包完整性
python3 $PY info ~/Desktop/wb-backup-*.tar.gz --verify
# → "[OK] 完整性校验通过" 才算成功

# ④ 传输备份包（U盘 / 加密云盘 / 局域网）
```

### 导入（目标设备）

```bash
# ① 查看包信息（源平台 / 模块 / 是否脱敏）
python3 $PY info <pkg.tar.gz>

# ② 预览（影子恢复：完整执行合并+映射+校验，但零写入）
python3 $PY restore <pkg.tar.gz> --dry-run [--auto-map]

# ③ 正式恢复
python3 $PY restore <pkg.tar.gz> --conflict merge|overwrite|rename [--auto-map] [--target-user-id <uuid>]
#    - 跨设备 / 跨账号 / 跨系统迁移务必加 --auto-map
#    - 恢复前自动创建回滚快照（--no-backup 可跳过，不推荐）

# ④ 查看校验结果（sessions / projects / tasks / user_id 一致性）

# ⑤ 如结果不符合预期，撤销本次恢复：
python3 $PY rollback --latest

# ⑥ 重启 WorkBuddy 生效
```

### 中断处理

- 备份中断（退出码 4）：staging 保留，`backup --resume <BACKUP_ID>` 续传
- 恢复中断：数据库按表事务自动回滚，已应用改动用 `rollback --latest` 撤销
- merge / overwrite / rename 三种策略均为幂等语义，中断后直接重跑同一命令也安全

---

## 同设备多账号切换

### 与跨设备迁移的区别

| 维度 | 跨设备迁移 `backup` + `restore` | 同设备多账号切换 `switch` |
|------|--------------------------------|--------------------------|
| 场景 | 换机器 / 重装 / 换新账号首次导入 | 同一台机器在 A、B 账号之间来回切 |
| 数据形态 | 单向导入，源包只读 | 双向：切走→切片，切回→还原 |
| 隔离 | 无（合入同一份数据） | 按账号隔离区存放，互不覆盖 |
| 可重复 | 幂等重跑 | 可反复来回切 |
| 目标账号 | 可以是本机从未登录的账号 | 必须是 `accounts` 能列出的账号 |

### 使用步骤

```bash
# ① 列出本机账号（只读，安全）
wb-migrate accounts

# ② 预览切换（零写入，强烈建议先跑）
wb-migrate switch --to <USER_ID> --dry-run

# ③ 正式切换
wb-migrate switch --to <USER_ID>

# ④ 需要时切回
wb-migrate switch --to <原USER_ID>
```

### 账号识别方式

多源聚合，任一命中即纳入清单：

| 来源 | 信号 | 权重 |
|------|------|------|
| 强信号目录 | `connectors/<uid>`、`storage/user-<uid>`、`memory/<uid>_memory.md`、`memery/<uid>_memery.md`、`plugin-account-state/<uid>.json`、`<uid>/` | 归属判定 |
| 弱信号目录 | `tasks/<uid>`、`todos/`、`plans/`、`experts/`、`local_storage/` | 仅佐证 |
| 数据库 | `sessions.user_id` 分组（会话数 + 最近活跃时间） | 归属判定 + 活跃排序 |
| 隔离区 | `.wb-migrate-accounts/<uid>/account.json` | 归属判定（已切走的账号必须仍可切回） |

命名识别用严格 UUID 正则（`^user-<uuid>` / `<uuid>_memory.md` / `<uuid>.json`…），排除产物/资源类 UUID（artifact-index、file-history 等），避免误判。

当前账号判定优先级：`--from` 显式指定 > 上次切换写入的 `state=active` 记录 > 综合活跃时间（DB 最近会话时间戳与专属文件 mtime 取最大值）。

### 隔离区结构

`~/.workbuddy/.wb-migrate-accounts/<uid>/`

```
<uid>/
├── account.json      # user_id / state(active|stashed) / last_op / 切换历史
├── data/             # 账号专属数据切片（切走时移入，切回时还原）
│   ├── connectors/<uid>/
│   ├── memory/<uid>_memory.md
│   └── storage/user-<uid>/
├── shared/           # 切走瞬间的全局配置快照（settings.json / SOUL.md / mcp.json …）
└── db-rows.json      # --db-mode stash 时抽取的数据库行
```

同一时刻只有一份数据在活动位置，另一份在隔离区，因此不会出现互相覆盖或污染。
切片陈旧副本处理：切回后以活动位置为准，下次切走时重新切片覆盖；**只有在整次切换全部成功后**才清空目标切片——这样中途失败回滚时，数据不会两头皆空。

### 同名配置冲突策略（`--config-policy`）

| 策略 | 行为 |
|------|------|
| `keep`（默认） | 保留当前值，目标账号快照不改任何东西 |
| `overwrite` | 用目标账号的快照覆盖当前全局配置（原文件入备份 + 回滚日志） |
| `merge` | JSON 浅合并：目标账号的值优先，当前独有键保留；非 JSON 文件写 `*.imported-{ts}` 副本 |

### 数据库模式（`--db-mode`）

| 模式 | 行为 | 适用 |
|------|------|------|
| `keep`（默认） | 主库不动，两个账号的行共存（文件系统层隔离） | 日常切换，最安全 |
| `merge` | 目标账号缺失的行补齐，已存在行不修改 | 希望切过去就看到目标账号完整历史 |
| `stash` | 切走的账号行抽到 `db-rows.json` 并从主库删除；切回时还原 | 强隔离 / 隐私要求高 |

`stash` 模式下账号在活动位置可能再无痕迹，但隔离区记录保证它仍在 `accounts` 清单里，随时可切回。

### 可靠性保证

- 切换前自动备份当前账号数据到 `.wb-migrate-history/switch-{id}/pre-switch-account-{uid}/`
- 全过程复用 `RollbackJournal`：文件移动/写入/覆盖、数据库行删除与插入均可逆
- 任一阶段抛异常 → 自动回滚（`--no-auto-rollback` 可关闭，不推荐）
- Windows 下检测到 WorkBuddy 仍在运行直接阻断（退出码 3）；macOS/Linux 告警后继续
- 每次切换输出 `switch-report.json`：`from`/`to`/策略/各阶段计数/`errors[]`（含 phase 与失败原因）

### 输出示例

```
[1/9] 环境与账号解析
切换方向: bbbb2222-… -> aaaa1111-…
共享配置策略: keep   数据库模式: keep
[3/9] 备份当前账号数据
    已备份 6 个文件, 2 行到 pre-switch-account-bbbb2222-…
[4/9] 当前账号数据切片（移入隔离区）
    切片 4 项, 跳过 0 项
[5/9] 还原目标账号数据
    还原动作: created=4
[6/9] 共享配置冲突处理 (keep)
    保留/覆盖/合并统计: skip=2 create=0 overwrite=0 merge=0 rename=0
[7/9] 数据库账号数据 (keep)
    keep 模式: 主库保持不变
== 切换完成: bbbb2222-… -> aaaa1111-… ==
  成功项: 切片 4 项 / 还原 4 / 写回行 0
  跳过项: 2 (同名未覆盖 / 目标已存在 / 非当前账号数据)
  回滚记录: switch-20261022-135701-2905
  报告文件: .wb-migrate-history/switch-20261022-135701-2905/switch-report.json
```

---

## 冲突策略

`--conflict merge|overwrite|rename`（默认 merge）

| 层 | merge | overwrite | rename |
|----|-------|-----------|--------|
| 数据库行 | INSERT OR IGNORE（跳过已存在） | 覆盖已存在行，旧值写入回滚日志 | 冲突行改 pk 为 `{pk}.imported-{ts}` 导入，绝不改动目标已有行 |
| session | 跳过 | 覆盖 | 新 sid 导入；projects/tasks 目录与 session_usage 等关联表**同步改名** |
| 文件 | 已存在跳过 | 覆盖（原文件先入回滚快照） | 写为 `{name}.imported-{ts}` |
| 目录树 | 递归合并，缺则补 | 递归覆盖 | 冲突文件加后缀 |
| edge-sync 库 | 已存在跳过 | 备份后替换 | 复制为 `*.imported-{ts}` |

数据库合并细节：

- 10 张业务表按列交集插入：源多出的列丢弃（记日志），目标独有的 NOT NULL 无默认列填类型零值 → 跨版本 schema 安全
- 目标库缺表默认 WARN 跳过（建议先启动一次 WorkBuddy 让 App 建表），`--create-missing-tables` 按源 DDL 自动建表
- `__workbuddy_drizzle_migrations` **永不合入**（合入会让目标 App 误判迁移状态）
- 逐表独立事务，单表失败不影响其他表

---

## 回滚机制

每次 restore（非 dry-run、未 `--no-backup`）在**任何写入之前**创建：

```
~/.workbuddy/.wb-migrate-history/restore-{时间戳}-{随机}/
├── rollback.json      # 操作清单 (file_created/file_overwritten/db_rows_inserted/
│                      #   db_row_overwritten/db_row_renamed/db_rows_updated/
│                      #   dir_renamed/dir_created/dir_merged/file_moved + sha256)
├── workbuddy.db.pre   # 恢复前的数据库快照 (SQLite backup API)
└── files/…            # 被覆盖文件的原件
```

`rollback` 按 seq **倒序**执行：改名/更新恢复原值 → 插入行删除（删除前校验行 sha256，防止误删恢复后又被用户改过的数据）→ 覆盖文件还原（校验 sha256_before）→ 新建文件删除 → 空目录移除。

```bash
rollback --list                 # 列出记录（时间/来源包/策略/操作数）
rollback --latest               # 撤销最近一次
rollback --id <rollback_id>     # 撤销指定记录
rollback --dry-run              # 预览
rollback --hard                 # 硬回滚: 用 workbuddy.db.pre 整体替换数据库
rollback --prune --keep 5       # 清理历史快照（默认保留 5 个）
```

> 注意：回滚目录为 `.wb-migrate-history/`，与 App 自有的 `.migration-history/` 无关。

---

## 脱敏与安全

默认开启（`--no-redact` 关闭，仅建议自用迁移时使用）：

| 目标 | 处理 |
|------|------|
| `connectors/*/.credentials.v3.json`、`connector-states*.json` | 敏感键（token/secret/ct/iv/tag/envelope/salt 等）→ `***WB-REDACTED***`，保留 JSON 结构 |
| `connectors/*/.master.key`、`keyblob`、`device-id`、`login-device-nonce` | 整文件省略（记入 manifest `omitted_files`） |
| `mcp.json` | URL query（apikey/token/key…）与 headers 值脱敏 |
| `settings.json` | claw botToken/botSecret（$wbEncrypted envelope）等脱敏 |
| 兜底 | 任何 `.json` 中键名含 token/secret/password/apikey/credential 等子串且值为标量 → 占位符 |

- 脱敏在打包时**内存中完成**，不修改磁盘源文件
- manifest 记录 `redacted: true`；info/restore 均提示"凭据不可还原，连接器需在目标端重新授权"
- 需要完整凭据迁移时，回源机用 `--no-redact` 重新导出

其他安全措施：

- 备份包恢复前必须通过 sha256 校验链（退出码 2 拒绝）
- tar 解包拦截绝对路径/`..`穿越/符号链接/设备文件
- 原子写入：`.part` + `os.replace`，中断不产生损坏包
- 传输建议：加密通道；备份包可能含完整对话历史

---

---

## 环境兼容性

### 支持范围

| 项目 | 支持范围 | 实测情况 |
|------|----------|----------|
| **WorkBuddy（国内版）** | **5.6.0 – 5.9.0** 完整支持 | **5.9.0** 本机全量备份 100.3MB / 2494 文件 + 恢复 + 回滚通过 |
| | 5.0 – 5.5（legacy） | 可备份；恢复到新版按列交集处理，输出 WARN 不阻断 |
| | ≥ 5.9.1（newer） | 正常备份；因目录可能新增，恢复后建议人工抽查，输出 WARN 不阻断 |
| | < 5.0（too-old） | 仅建议备份留存，`restore` 会给出阻断性提示 |
| **备份包格式** | v3（本次产物），可读 v1 / v2 旧包 | 旧包自动合成清单（legacy 模式） |
| **Python** | 3.9 – 3.13（**仅标准库，零第三方依赖，无需 pip install**） | 3.13.12 实测；代码不使用 `match` / `X\|Y` 等 3.10+ 语法 |
| **macOS** | 12 Monterey 及以上（Intel / Apple Silicon） | macOS 26.x arm64 实测 |
| **Windows** | **Windows 10 1809（build 17763）及以上、Windows 11 全版本、Windows Server 2019+** | 由 `selftest` + 兼容层静态/动态规则覆盖（见下方验证方式） |
| **Linux** | Ubuntu 20.04+ / 内核 4.15+（best-effort） | 未实机验证，路径与安全策略走通用分支 |
| **文件系统** | NTFS / APFS（默认大小写不敏感）/ ext4 均可 | 大小写不敏感时自动走 rename 兜底 |

版本判定由常量集中定义：`WB_MIN_VERSION=5.6.0`、`WB_TARGET_VERSION=5.9.0`、
`PY_MIN_VERSION=(3,9)`、`WINDOWS_MIN_BUILD=17763`。

版本检测顺序（三路兜底，任一失败都不影响主流程）：

1. `~/.workbuddy/startup-seed-version.json` → `appVersion`（**三平台一致**，本机读出 `5.9.0`）
2. 应用安装目录内的 Electron `package.json`
   - Windows：`%LOCALAPPDATA%\Programs\WorkBuddy\...`、`%LOCALAPPDATA%\WorkBuddy\app-<ver>\resources\app\package.json`（Squirrel 更新目录一并探测）
   - macOS：`/Applications/WorkBuddy.app/Contents/Resources/app/package.json`
   - Linux：`/opt/WorkBuddy`、`/usr/share/WorkBuddy`、`~/.local/share/WorkBuddy`
3. macOS `Info.plist`（`defaults read`） / Linux 包管理器（rpm、dpkg-query）

检测结果写入备份包 `manifest.json → source.wb_version`，恢复时与本机版本比对并提示。

### WorkBuddy 5.9 的变更点与对现有代码的影响

以下为在本机（5.9.0）与留存的历史数据副本逐项比对得到的**实测差异**：

| 变更项 | 5.9 实测情况 | 对代码的影响 | 处理 |
|--------|--------------|--------------|------|
| `sessions` 新增 10 列 | `transport`、`conversation_origin`、`visibility`、`group_id`、`group_title`、`agent_dirty`、`agent_dirty_at`、`agent_last_synced`、`verified_at`、`unread` | 源/目标列数不一致会导致 INSERT 失败 | **无需改动**：合并引擎取两库列交集，差异列保留目标端原值 |
| `automations` 新增 3 列 | `reasoning_effort`、`workspace_scope`（值 `workspace`/`task`，非路径）、`wecom_bot_chat_id` | 同上 | 同上；`workspace_scope` 非路径，不参与 `--auto-map` 映射 |
| `automation_runs` 新增 2 列 | `failure_code`、`reason_code` | 同上 | 同上 |
| drizzle 迁移记录 | 18 → 19 条 | 属数据库内部 migration 状态，若合入会干扰目标端 | **已处理**：`DB_TABLES_SKIP` 跳过 `__workbuddy_drizzle_migrations` |
| 新增数据目录 | `blobs`、`artifact-index`、`media-index`、`file-history`、`changes-index`、`changes-detail`、`file-tree-manifests`、`shell-snapshots`、`inspiration`、`project-resources`、`todos` | 若未归类会被漏备份 | **已处理**：分别纳入 `artifacts` / `assistant` / `tasks` / `config` 模块，本机扫描均已计数 |
| 旧版根级账号目录 `<uuid>/` | 遗留 `969f6e18-…`、`de4ee3c0-…`（无 `user-` 前缀） | 之前列为"未归类"，助理数据会被漏掉 | **已处理**：新增动态标记 `@uuid-dirs`，按 UUID 规则自动展开进 `assistant`（本机 assistant 从 16 → 20 文件） |
| `desktop_conversation_migrated` | 时间戳标记文件（2026-08-10） | **跨机有害**：迁入目标端后，目标端会误认为桌面端历史会话已迁移过而跳过导入 | **已处理**：加入全局排除清单 |
| `.migration-history/`、`.workbuddy-sqlite-migrations/` | 迁移执行历史与 schema SQL | 随 App 版本变化，跨机恢复无用 | **已处理**：加入排除清单 |
| 运行时状态文件 | `epoch-marker-*.json`、`install-timing-reported`、`last-launch.json`、`tencent-docs-engine.port`、`usage-log.json`、`qimei-cache.json`、`plugin-status-daily.json`、`mcp-tool-list.json`、`extension-registry`、`external-packages` | 机器态 / 遥测缓存，恢复后会污染目标端状态 | **已处理**：加入排除清单 |
| `projects/<encoded>` 目录编码规则 | 规则**未变化** | 若变更将导致会话找不到 project | **已验证**：本机 122 个 sessions.cwd ↔ 122 个 projects 目录，命中率 122/122（含中文路径） |
| `migration_meta` 内 `legacy_*_migration` | 含源机绝对路径与历史 userId | 属历史事实，不应重映射 | 保持原样整行合入（不参与 `--auto-map`） |

**结论**：由于备份侧采用"模块通配 + 动态标记展开 + 全局排除"，恢复侧采用"列交集 + 白名单解包 + JSON 感知重写"，
5.9 的新增列与新增目录对现有代码是**零改造成本**的；真正需要干预的只有"跨机有害的迁移标记"和"旧版遗留账号目录"两项，均已在 v3.1.0 修正。

### 平台差异处理清单

| 差异点 | Windows | macOS / Linux | 处理方式 |
|--------|---------|---------------|----------|
| **路径分隔符** | `\`，另有盘符 `C:`、UNC `\\server\share` | `/` | 包内统一以 POSIX 相对路径存储；写出时用 `safe_join()` 逐段拼接，不依赖 `os.sep` 字符串拼接 |
| **路径跨 OS 语义** | `C:\Users\Alice\x` | `/Users/alice/x` | `--auto-map` 归一化前缀后比较（含盘符），未归一化不重映射 |
| **换行符** | 文本模式写入会把 `\n` 转成 `\r\n` | 保持 `\n` | **统一 `write_text_lf()`（`newline="\n"`）写入**，避免 CRLF 篡改 JSON/代码内容导致 sha256 校验失败；`selftest C2` 断言包内无 `\r\n` |
| **字符编码（控制台）** | 默认 GBK，中文路径/输出会乱码或抛 `UnicodeEncodeError` | UTF-8 | `sys.stdout/stderr` 强制 `reconfigure(encoding="utf-8", errors="replace")`；`.bat` 追加 `chcp 65001` + `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` |
| **字符编码（文件名）** | 可能出现无法 UTF-8 编码的 surrogate | 少见 | 所有输出到日志/清单的字符串经 `surrogate_safe()` 兜底 |
| **编码（文件内容）** | 同上 | 同上 | 读取 JSON 统一 `utf-8-sig`（自动去 BOM），写入用无 BOM UTF-8 |
| **长路径（> 260 字符）** | NTFS 受 MAX_PATH 限制 | 限制宽松 | `fs_path()` 在 Windows 且路径 ≥ 240 时自动加 `\\?\`（UNC 转 `\\?\UNC\`）前缀；未开启注册表 `LongPathsEnabled` 也能工作 |
| **保留设备名 / 非法字符** | `CON`/`PRN`/`AUX`/`NUL`/`COM1-9`/`LPT1-9`、`<>:"\|?*`、结尾空格或点 | 无限制 | `sanitize_windows_name()` 净化（`CON` → `_CON`，`bad<>:` → `bad___`），改写时输出 WARN；`selftest C3` 覆盖 |
| **大小写不敏感** | NTFS 默认不敏感 | APFS 默认**也不敏感**；ext4 敏感 | `fs_case_sensitive()` 运行时探测（一次性缓存），`ci_key()` 据此判定同名冲突，冲突项自动追加 `.imported-{ts}` |
| **只读属性 / ACL** | 目标文件只读时 `os.replace` 会抛 `PermissionError` | 少见 | `ensure_writable()` 前置提升；`rmtree_safe()` 带 `onerror` 回调重试 |
| **符号链接 / junction** | 创建链接需要权限，提取会失败 | 可创建 | 打包时跳过符号链接目录与断链（链接文件按真实内容入库）；解包成员含链接/设备时直接拒绝 |
| **文件被占用** | WorkBuddy 运行时会锁住 `workbuddy.db`，替换必然失败 | SQLite WAL 较为宽容 | `restore` 前调用 `check_app_running()`：Windows 上（tasklist）**直接中断并提示退出托盘**，其他平台 WARN |
| **脚本 / 命令行调用** | `python scripts\wb.py`（或 `python wb_migrate.py` / `py -3`） | `python3 scripts/wb.py`（或 `python3 wb_migrate.py`） | 见下方"Windows 使用说明"；`wb.py` 自带参数透传与退出码透传，三端同一条命令 |
| **依赖安装** | 无需任何依赖 | 同 | 仅标准库；任何时候都**不需要** `pip install` |
| **临时目录 / 原子写入** | `%TEMP%`，与工作目录可能不同盘 | `/tmp` | 统一用 `tempfile.mkdtemp()`；临时文件名截断到 60 字符（`_wb_tmp()`）控制在 MAX_PATH 内；`.part` + `os.replace` 原子落盘 |
| **OS 专有特性** | 不使用 | 不使用 | 代码中无 `fcntl` / `pwd` / `grp` / `termios` / `os.getuid` / `pty` / shell 外部命令，仅跨平台 `subprocess`（且全部包在异常兜底里） |
| **进程管理信号** | 无 `SIGQUIT`，无 pkill | 有 | 仅注册 `SIGINT`（失败静默），二次中断才强制退出 |

### Windows 使用说明

```bat
REM ① 安装 Python 3.9+ (python.org 或 Microsoft Store), 无需任何第三方库
REM    python --version   应显示 3.9 及以上

REM ② 环境兼容性自检 (推荐每台新机器先跑一次)
python %USERPROFILE%\.workbuddy\skills\wb-migrate\scripts\wb.py compat

REM ③ 端到端一致性自检 (18 项用例, 秒级)
python %USERPROFILE%\.workbuddy\skills\wb-migrate\scripts\wb.py selftest

REM ④ 备份 (注意: 全局参数必须放在子命令之前)
python %USERPROFILE%\.workbuddy\skills\wb-migrate\scripts\wb.py backup -o D:\Backup

REM ⑤ 恢复前务必完全退出 WorkBuddy (含托盘图标), 然后:
python %USERPROFILE%\.workbuddy\skills\wb-migrate\scripts\wb.py restore D:\Backup\wb-backup-xxx.tar.gz --conflict merge --auto-map
python %USERPROFILE%\.workbuddy\skills\wb-migrate\scripts\wb.py rollback --latest
```

嫌路径长就先设个别名（PowerShell，写进 `$PROFILE` 永久生效）：

```powershell
Set-Alias wbm "$env:USERPROFILE\.workbuddy\skills\wb-migrate\scripts\wb.py"
wbm compat
```

说明与注意事项：

- **解释器**：`wb.py` 会在版本不足 3.9 时依次探测 `python3` → `python` → `py -3` 自动重启自己；全都没有时给出安装指引并返回退出码 1。
- **编码**：`wb.py` 强制 `stdout/stderr` 走 UTF-8 + `errors=replace`，中文路径与日志不会中断迁移；PowerShell 用户如仍乱码，
  可先执行 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`。
- **退出码**：`0` 成功 / `1` 错误 / `2` 校验失败（包被拒绝，未写入任何数据）/ `3` 权限不足 / `4` 用户中断。
- **不要以管理员运行**：会改变 `USERPROFILE` 指向的对象，导致备份/恢复的目标目录不是日常使用的那个。
- **路径含空格**：请用引号包裹，例如 `restore "D:\My Backup\wb-backup-xxx.tar.gz"`。
- **OneDrive / 网盘目录**：避免把 `.workbuddy` 放在未同步完成的网盘目录内，恢复时会有文件占用。
- **安全软件**：首次运行可能被拦截，放行或临时关闭实时保护即可。
- **PowerShell 7 / 旧 conhost**：输出为纯文本（无 ANSI 颜色、无 emoji），不存在控制字符乱码问题。

#### 免手动自动恢复（wb_auto.py，v3.3 新增）

Windows 下 WorkBuddy 运行时 `workbuddy.db` 被进程锁住，直接 `restore` 会被 `EXIT_PERMISSION(3)` 拒绝；传统做法是「手动退出 WorkBuddy → 手动双击 bat」，很容易漏掉托盘图标。`scripts/wb_auto.py` 把这一步也自动化了：注册一个**计划任务作为看门狗**（Task Scheduler 持有进程，不随会话退出），轮询到 WorkBuddy 进程消失、句柄释放后自动执行恢复，恢复完合并插件注册表并可选自动重开，全程写日志，结束后自删计划任务。

```bat
REM A) 注册看门狗（推荐）：立刻返回，之后你随手退出 WorkBuddy 即可
python wb-migrate\scripts\wb_auto.py restore D:\Backup\wb-backup-xxx.tar.gz --relaunch

REM B) 已经退出 WorkBuddy，立即执行
python wb-migrate\scripts\wb_auto.py restore D:\Backup\wb-backup-xxx.tar.gz --now

REM 查看看门狗状态 / 取消
python wb-migrate\scripts\wb_auto.py status
python wb-migrate\scripts\wb_auto.py cancel
```

- 需要 Python 3.9+；计划任务用当前用户身份注册，**不需要管理员**。
- 恢复仍在原有回滚机制之内：出错可 `python scripts\wb.py rollback --latest`。
- `__watch` 是计划任务调用的内部入口，不要手动执行。
- macOS / Linux 不需要本脚本，退出 WorkBuddy 后直接 `restore` 即可（无进程锁）。

### 验证方式

**① 环境自检（新机器第一件事）**

```bash
wb-migrate compat            # 机器可读: compat --json
```

输出：系统/系统版本、Python、SQLite、文件编码与控制台编码、文件系统大小写敏感性、
Windows 长路径开关、`~/.workbuddy` 是否存在、WorkBuddy 版本与其判定（supported / newer / legacy / too-old / unknown）、
磁盘可用、WorkBuddy 是否在运行 + 警告汇总。存在阻断项时退出码 1。

**② 端到端一致性自检（跨平台同一套用例）**

```bash
wb-migrate selftest
```

18 项用例（本机实测 18/18 通过，约 4 秒），在同一进程的临时目录内完成：构造 5.9 风格 fixture →
打包 → 校验 → 脱敏断言 → dry-run → 恢复到"低版本+不同账号"目标机 → 三冲突策略 → 回滚 →
破损包拒绝 → 平台路径适配 → 账号识别 → 双向切换与隔离 → 冲突策略与数据库模式。用例与操作系统无关的部分（Windows 文件名规则、projects 编码、
版本判定、列交集合并）在任何平台都按同一套断言执行。

| 用例 | 断言内容 |
|------|----------|
| C1 / C5 / C6 | Python 版本、WorkBuddy 版本判定、版本检测三路兜底 |
| C2 | 文本写入**恒定 LF**（Windows 上不会出现 `\r\n` 破坏 sha256） |
| C3 | Windows 文件名净化规则（保留名 / 非法字符 / 结尾点） |
| C4 | `projects` 目录编码（POSIX 与 Windows 双向，含盘符小写） |
| C7 / C8 | 打包成功、清单写入 `wb_version`、`--verify` 通过 |
| C9 | 凭据脱敏、`.master.key` 省略、迁移标记不入库、包内无 CRLF |
| C10 | `dry-run` 前后哈希树完全一致（零写入） |
| C11 | 跨版本（5.9 → 5.8 schema）+ 跨账号 + 跨 OS 路径恢复后文件与数据完整 |
| C12 | merge / overwrite / rename 三种策略行为正确 |
| C13 | 回滚按逆序撤销，文件数回落 |
| C14 | 截断包与位翻转包均被拒绝（退出码 2），且未写入任何目标数据 |
| C15 | 平台适配层：Windows 长路径前缀生效 / 非 Windows 不改写路径 |
| C16 | 账号识别：双账号均被识别、当前活跃账号唯一 |
| C17 | 双向切换：B→A 后 A 就位且 B 已隔离，A→B 后 B 还原且 A 已隔离 |
| C18 | `--config-policy overwrite` 生效；`--db-mode stash` 隔离/还原会话行；`switch --dry-run` 零写入 |

**③ 真机验证（本机 5.9.0 已完成）**

```bash
wb-migrate scan                                   # 2498 文件 / 293.9MB / 12 模块, 版本 5.9.0 supported
wb-migrate backup -o /tmp/wb-v31                  # 100.3MB, 2494 文件, 脱敏 18 个文件 + 省略 3 个 .master.key
wb-migrate info /tmp/wb-v31/*.tar.gz --verify     # [OK] 完整性校验通过
wb-migrate restore <pkg> --auto-map --dry-run     # 2494 项 dry-created, 零写入
```

Windows 端建议按同样顺序执行一遍 `compat` → `selftest` → `scan` → `backup` → `info --verify`，
若 `selftest` 15/15、`scan` 能列出 12 个模块、`info --verify` 报 OK，即可确认与 macOS 端功能一致。

---

## 跨平台路径与账号映射

`--auto-map` 自动处理（在文件恢复后执行）：

| 映射项 | 说明 |
|--------|------|
| `sessions.cwd` | 源 home 前缀 → 目标 home 前缀（兼容 `/` 与 `\` 存储） |
| `workspaces.path` | 同上 |
| `workspace_trust_decisions.workspace_key` | 同上 |
| `automations.cwds`（JSON 数组） | 逐项映射 |
| `automation_runs.source_cwd` | 同上 |
| `projects/<encoded>` 目录 | 按新 cwd 重命名；冲突则合并（不删源，残余保留为 `*.wb-migrate-orig-{ts}`） |
| 分享文件 JSON | `cloudstudio-deploy-history/*.json` 内 absolutePath/workspacePath 重写 |
| 账号合并 | 源 `user_id` → 目标账号（按最近活跃推断，或 `--target-user-id` 指定）；**仅限备份包内出现的记录**，不会误改目标已有会话；`automations.owner_user_id` 同步 |

触发条件：源/目标 home 前缀归一化后不同——涵盖跨 OS、同系统换用户名（`/Users/alice` → `/Users/bob`）、Windows 换盘符。home 一致时仅执行账号合并。

路径编码规则（5.8.3 实测）：`/Users/li/WorkBuddy/x` → `Users-li-WorkBuddy-x`；`C:\Users\Alice\WorkBuddy\x` → `c-Users-Alice-WorkBuddy-x`（去前导 `/`、`/`→`-`、盘符小写、中文保留）。

---

## 架构设计

```
wb-migrate/
├── SKILL.md                    # Skill 定义 (触发/用法/模块表)
├── README.md                   # 本文档
├── agents/openai.yaml          # 可选 UI 元数据
├── scripts/
│   ├── wb_migrate.py           # 核心引擎 (单文件, 仅标准库, Python 3.9+)
│   ├── wb_auto.py              # Windows 免手动自动恢复编排器 (看门狗 + 自动重开)
│   └── wb.py                   # 跨平台启动器 (解释器检查 + UTF-8 兜底 + 参数透传)
├── wb-migrate.sh               # [仓库版] macOS/Linux 薄封装, SkillHub 分发包不含
└── wb-migrate.bat              # [仓库版] Windows 薄封装, SkillHub 分发包不含
```

引擎分区（单文件内按区块组织）：

```
CLI (main/build_parser)
 ├── scan / modules / info / verify / rollback 命令
 ├── 备份引擎 create_backup ── 模块引擎 (MODULES/iter_module_files)
 │                            ── 脱敏引擎 (redact_bytes/_redact_json_obj)
 │                            ── 校验清单 (checksums.sha256/manifest)
 │                            ── staging + 原子 gzip + --resume
 └── 恢复引擎 restore_backup
      ├── verify_archive (sha256 校验链)
      ├── safe_extract_tar (安全解包)
      ├── RollbackJournal (操作日志 + 回滚)
      ├── 数据库合并 (列交集 + 三策略 + 10 表)
      ├── 文件恢复 (restore_file/_restore_dir 三策略)
      ├── 跨平台映射 (remap_*/merge_user_ids/ensure_workspace_dirs)
      └── validate_restore (恢复后校验)
```

数据流：

```
备份: ~/.workbuddy → 模块规划 → 内存脱敏 → staging tar → checksums+manifest → 原子 gzip
恢复: 校验 → 解包 → pre-restore 快照 → DB 合并 → 文件恢复 → 路径/账号映射 → 校验
回滚: rollback.json 倒序撤销 (sha256 校验防误删)
```

---

## API 参考

### 命令与参数

| 命令 | 参数 |
|------|------|
| `scan` | `--modules L` `--include-large` |
| `modules` | `--json` |
| `backup` | `-o/--output DIR`、`--modules L`、`--exclude-modules L`、`--exclude GLOB`(多次)、`--include-large`、`--include-other`、`--include-self`、`--no-redact`、`--level 1-9`(默认6)、`--dry-run`、`--resume ID` |
| `info` | `archive`、`--verify` |
| `verify` | `archive` |
| `restore` / `migrate` | `archive`、`--conflict merge\|overwrite\|rename`、`--modules L`、`--dry-run`、`--auto-map`、`--target-user-id UUID`、`--no-backup`、`--allow-unknown`、`--all-tables`、`--create-missing-tables` |
| `rollback` | `--list`、`--latest`、`--id ID`、`--dry-run`、`--hard`、`--prune`、`--keep N` |
| `accounts` | `--json` — 列出本机账号（同设备多账号切换，只读） |
| `switch` | `--to USER_ID`(必填)、`--from USER_ID`、`--config-policy keep\|overwrite\|merge`、`--db-mode keep\|merge\|stash`、`--dry-run`、`--no-auto-rollback`、`--report-file PATH` |
| `compat` | `--json` — 环境兼容性自检报告 |
| `selftest` | 无参数 — 跨平台端到端一致性自检（18 项用例，含 3 项账号切换） |

全局（子命令之前）：`--wb-home PATH`（或环境变量 `WB_MIGRATE_HOME`）、`-v/--verbose`、`-q/--quiet`、`--version`。

退出码：0 成功 / 1 通用错误 / 2 校验失败 / 3 权限不足 / 4 用户中断。

### 环境变量

| 变量 | 说明 |
|------|------|
| `WB_MIGRATE_HOME` | 覆盖 WorkBuddy 数据目录（默认 `~/.workbuddy`，测试用） |
| `PYTHONIOENCODING` | Windows `.bat` 内置为 `utf-8` |
| `PYTHONUTF8` | Windows `.bat` 内置为 `1`，强制 UTF-8 模式 |

---

## 数据隔离说明

| 数据类型 | 隔离方式 | 跨账号可见 | 脱敏 |
|----------|---------|-----------|------|
| Sessions | `user_id` 字段 | `--auto-map` 合并后可见 | — |
| Tasks / Automations | session / owner_user_id | 随 session | — |
| user-*/ 助理目录、memory/ | 按 UUID 命名 | 共享 | — |
| Skills / Projects / Connectors / 产物 | 无隔离 | 共享 | connectors/mcp.json/settings.json 脱敏 |
| **账号切片（v3.2）** | `.wb-migrate-accounts/<uid>/data/` 物理隔离 | 仅该账号活跃时可见 | 不脱敏（本机数据，非导出包） |

5.8.x 结构要点：`user-<uuid>-<nanoid>/`（新版用户目录）与根级扁平五件套并存，两者均备份（assistant 模块）；记忆在 `memory/<uuid>_memory.md` 与历史拼写目录 `memery/`；第二数据库 `edge-sync-mapping-v4.db` 独立复制不合并。

---

## 故障排查

| 症状 | 原因与解决 |
|------|-----------|
| 恢复后任务可见但报"工作目录已被删除" | 跨平台路径未映射；用 `restore <pkg> --auto-map` 重新恢复（merge 幂等） |
| Skills 恢复了但对话历史看不到 | ① session 的 user_id 不是当前账号 → `--auto-map`；② cwd 路径本地不存在 → `--auto-map` |
| `[SKIP] 目标库缺少表 xxx` | 目标库 schema 旧；先启动一次 WorkBuddy 让 App 建表，或 `--create-missing-tables` |
| 校验失败退出码 2 | 包损坏或被篡改；重新创建备份。中断的备份不会产生损坏包（原子写入） |
| `[校验失败] …清单摘要不匹配` | manifest/checksums 被修改过；以源端重新导出的包为准 |
| 恢复中断想放弃 | `rollback --latest` 撤销已应用改动 |
| 备份中断 | 按提示 `backup --resume <BACKUP_ID>` 续传；或忽略 staging（自动保留 7 天后可手动删 `~/.workbuddy/.wb-migrate-tmp/`） |
| 权限不足退出码 3 | WorkBuddy 沙箱模式弹窗点「允许」，或切「完全放开」；写目录见错误信息 |
| Windows 控制台乱码 | 已强制 UTF-8 输出（`errors=replace`）；如仍异常改用 PowerShell 或重定向到文件 |
| 旧包恢复告警"未知顶层条目" | v2 包内含白名单外目录（如 binaries/）；确需导入加 `--allow-unknown` |
| 回滚提示 `[SKIP] 行已变化` | 恢复后该行又被修改过，为保护你的改动未删除；确认不要可 `rollback --hard` |

日志位置：`~/.workbuddy/logs/wb-migrate-{时间戳}.log`（含 DEBUG 级完整过程）。

---

## 部署与安装

```bash
# 手动安装 / 升级
cp -r wb-migrate ~/.workbuddy/skills/
# 验证
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py --version
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py scan
```

- 依赖：Python 3.9+（仅标准库）；**无需 pip install 任何包**
- 平台：macOS 12+ / Windows 10 1809+ / Linux（详见[环境兼容性](#环境兼容性)）
- Windows：`python %USERPROFILE%\.workbuddy\skills\wb-migrate\scripts\wb.py <命令>`（自动处理编码、解释器探测与退出码）；`wb-migrate.bat` 仅仓库版附带
- 升级覆盖安装即可，旧备份包 (v1/v2) 仍可恢复
- 新机器建议先跑：`wb-migrate compat` 与 `wb-migrate selftest`

---

## FAQ

**Q: 脱敏后的凭据能还原吗？**
不能。占位符单向替换。需要完整凭据迁移时在源机用 `--no-redact` 重新备份，并妥善保管包文件（含明文密钥）。

**Q: 切换账号后连接器要重新授权吗？**
同设备切换不需要——`connectors/<uid>` 随账号整体切片/还原，凭据文件原样保留。跨设备恢复脱敏包才需要重新授权。

**Q: `switch` 提示"目标账号不在本机账号清单中"怎么办？**
说明该账号在本机从未留下数据（或已被 `stash` 完全隔离且隔离区被手工删除）。全新账号请用 `backup` + `restore --auto-map` 导入。

**Q: 切换会动到另一个账号的数据吗？**
不会。切走时只有当前账号的专属数据被移入隔离区，目标账号的数据从它自己的切片还原；同名全局配置按 `--config-policy` 处理，默认 `keep` 不动当前值。

**Q: 切换中途失败了，数据会丢吗？**
不会。切换前已备份当前账号，且任何阶段抛异常都会自动回滚（`--no-auto-rollback` 可关闭，不推荐）。回滚失败时按提示手工执行 `rollback --id switch-xxx`。

**Q: 隔离区会一直变大吗？**
每个账号的切片就是它自己的数据，不是副本累积。`data/` 在切回时会被清空（数据回到活动位置），`shared/` 是几 KB 的配置快照。

---

## 版本历史

| 版本 | 变更 |
|------|------|
| v3.3.0 | **专家模块与免手动恢复**：`experts` 模块补齐 `plugins/cache/experts`（5.9 起专家实体全部在此，原先只写 `experts` 会漏 100% 的专家），同时纳入 `plugins/marketplaces/experts`、`plugins/marketplaces/my-experts`，新增 `plugins/installed_plugins.json` / `configured-plugins.json` / `known_marketplaces.json` 注册表文件，并把 `.in_use`、`.downloaded_at` 列为机器态排除；新增 `scripts/wb_auto.py`（Windows 免手动自动恢复：注册 `schtasks` 看门狗 → 轮询进程消失 → 句柄释放后自动 restore → 合并专家插件注册表 → 可选 `--relaunch` 重开 → 自删任务，全程写日志）；Windows 控制台强制 UTF-8（`errors=replace`），中文路径/日志不再因 GBK 编解码中断迁移；新增跨平台启动器 `scripts/wb.py`（三端统一入口：Python 版本检查 + 自动换解释器重启、UTF-8 兜底、参数与退出码透传），SkillHub 分发包不再携带 `.bat` / `.sh`（市场不接受 `.bat`）|
| v3.2.0 | **新增同设备多账号切换**：`accounts` / `switch` 命令；按账号隔离区存储（data/shared/db-rows）；三种同名配置策略（keep/overwrite/merge）+ 三种数据库模式（keep/merge/stash）；切换前自动备份、失败自动回滚、`switch-report.json` 报告；账号识别多源聚合（强/弱信号目录 + DB + 隔离区）；当前账号判定支持切换记录与文件 mtime；`selftest` 扩至 18 项（新增 C16/C17/C18）；统一 `_mkdir` 走 `fs_path` 规避 Windows 长路径 |
| v3.1.0 | 环境兼容性：支持矩阵 5.6–5.9 与 Windows 10 1809+；版本检测三路兜底；Windows 适配层（长路径前缀/保留名净化/大小写不敏感兜底/只读提升/统一 LF/运行占用检测）；新增 `compat` 命令与 `selftest` 15 项用例；修正 5.9 机器态排除项与异 OS 工作区路径误建 |
| v3.0.0 | 结构化备份包 v3（manifest + checksums + database/ + modules/）、12 模块、三冲突策略、回滚日志、脱敏、进度日志与断点续传 |