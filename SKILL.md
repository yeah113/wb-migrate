---
name: wb-migrate
description: WorkBuddy 全局备份与迁移工具 v3.2 — 适配国内版 WorkBuddy 5.6-5.9（5.9.0 实测）。全量或按模块选择性导出对话、任务、记忆、配置、连接器、专家、定时任务、技能、产物、分享文件、应用、助理；带 sha256 校验清单的结构化备份包；恢复支持覆盖/合并/重命名三种冲突策略，导入前自动快照并可用 rollback 命令回滚；敏感信息（Token/密钥/凭据）默认脱敏。适用于备份 WorkBuddy 数据、迁移到新账号/新电脑、跨系统（macOS/Windows/Linux）迁移、修复跨平台路径错误、旧版备份包恢复等场景。
  另提供 compat（环境兼容性报告）、selftest（跨平台端到端一致性自检）、accounts / switch（同设备多账号切换与隔离）命令。
  **v3.3.0**：experts 模块补齐 `plugins/cache/experts`（5.9 专家实体真实位置）并新增插件注册表合并；新增 `wb_auto.py` 免手动自动恢复（退出即恢复 + 自动重开）；Windows 编码与不可读文件加固。
---

# wb-migrate v3.3.0 — WorkBuddy 全局备份与迁移

## 触发场景

- "备份 WorkBuddy 数据" / "导出 WorkBuddy 配置" / "数据迁移"
- "迁移到新账号/新电脑" / "换账号后恢复任务"
- **同设备多账号切换** / "换回原来的账号" / "两个账号的数据不要混在一起"
- 跨平台（macOS ↔ Windows ↔ Linux）迁移、路径错误修复
- 只迁移部分数据（如仅技能、仅对话）
- 恢复后需要撤销 / 回滚
- 处理旧版 wb-migrate (v1/v2) 创建的备份包

## 使用方法

### 1. 扫描当前数据（只读）
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py scan
```

### 2. 查看可用模块
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py modules
```

### 3. 创建备份（全量，默认脱敏）
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py backup --output ~/Desktop
```

### 4. 按模块选择性备份
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py backup -o ~/Desktop --modules conversations,tasks,memory
```

### 5. 查看备份信息并校验完整性
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py info <backup.tar.gz> --verify
```

### 6. 预览恢复（影子恢复，零写入）
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore <backup.tar.gz> --dry-run
```

### 7. 恢复（默认 merge，不覆盖已有数据）
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore <backup.tar.gz>
```

### 8. 指定冲突策略恢复
```bash
# overwrite: 覆盖已有数据（原数据自动备份，可回滚）
python3 ... restore <backup.tar.gz> --conflict overwrite
# rename: 冲突数据改名导入为 *.imported-{ts}，绝不改动目标已有数据
python3 ... restore <backup.tar.gz> --conflict rename
```

### 9. 跨平台 / 跨账号自动映射恢复（推荐）
```bash
python3 ... restore <backup.tar.gz> --auto-map
# 目标账号无任何会话、无法自动检测 user_id 时：
python3 ... restore <backup.tar.gz> --auto-map --target-user-id <uuid>
```
> 自动处理：sessions.cwd / workspaces / 信任决策 / 定时任务 cwds 的路径前缀映射（含同系统换用户名、Windows 换盘符）、projects 目录重命名、账号 user_id 合并（仅限备份包内的记录）、工作区与 project 目录补建。

### 10. 回滚一次恢复
```bash
python3 ... rollback --list                 # 列出可回滚记录
python3 ... rollback --latest               # 撤销最近一次恢复
python3 ... rollback --latest --dry-run     # 预览回滚
python3 ... rollback --prune --keep 5       # 清理历史快照
```

### 11. 中断后续传
```bash
# 备份被中断后（退出码 4），按提示的 BACKUP_ID 续传：
python3 ... backup -o ~/Desktop --resume <BACKUP_ID>
```

### 12. 环境兼容性自检（新机器推荐先跑）
```bash
python3 ... compat            # 报告 OS/Python/编码/文件系统/WorkBuddy 版本判定
python3 ... compat --json     # 机器可读输出
```

### 13. 端到端一致性自检（跨平台同一套用例）
```bash
python3 ... selftest          # 18 项用例: 打包/校验/脱敏/dry-run/跨版本恢复/三策略/回滚/破损拒绝/账号切换
```

### 14. 查看本机账号（同设备多账号切换 · 只读）
```bash
python3 ... accounts                # 列出本机所有账号、体量、切片状态、当前活跃
python3 ... accounts --json         # 机器可读
```

### 15. 同设备账号切换（Account A → B）
```bash
# 先预览（零写入，强烈建议先跑）
python3 ... switch --to <USER_ID> --dry-run

# 正式切换（默认: 共享配置 keep / 数据库 keep）
python3 ... switch --to <USER_ID>

# 显式指定策略
python3 ... switch --to <USER_ID> --config-policy merge --db-mode stash

# 指定源账号（自动识别不准时）
python3 ... switch --to <USER_ID> --from <USER_ID>
# 指定报告输出位置
python3 ... switch --to <USER_ID> --report-file ./switch-report.json
```

| 参数 | 取值 | 含义 |
|------|------|------|
| `--to` | USER_ID | **必填**，目标账号（须已在本机留下数据，或用 `accounts` 能列出） |
| `--from` | USER_ID | 源账号；省略时自动判定（切换记录 > 综合活跃时间） |
| `--config-policy` | `keep`(默认) / `overwrite` / `merge` | 同名全局配置冲突处理 |
| `--db-mode` | `keep`(默认) / `merge` / `stash` | 数据库账号数据处理 |
| `--dry-run` | — | 预览，零写入 |
| `--no-auto-rollback` | — | 关闭失败自动回滚（不推荐） |
| `--report-file` | 路径 | 额外输出一份 JSON 报告 |

`--db-mode` 差异：

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `keep` | 主库不动，两账号行共存（仅文件系统层隔离） | 日常切换，最安全 |
| `merge` | 目标账号缺失的行补齐，已有行不改 | 切换后想看到目标账号完整历史 |
| `stash` | 切走的账号行抽到隔离区并从主库删除，切回时还原 | 强隔离 / 隐私要求高 |

### Windows 用法
```bat
wb-migrate.bat compat
wb-migrate.bat selftest
wb-migrate.bat accounts
wb-migrate.bat switch --to <USER_ID> --dry-run
wb-migrate.bat backup -o D:\Backup
wb-migrate.bat restore "D:\Backup\wb-backup-xxx.tar.gz" --conflict merge --auto-map
```
> `.bat` 已处理：UTF-8 代码页、解释器探测（python → python3 → py -3）、退出码透传。
> 恢复前必须完全退出 WorkBuddy（含托盘图标），否则 Windows 下 `workbuddy.db` 被占用无法替换。

# Windows 入口见 skill 根目录 wb-migrate.bat
# 也可直接调用解释器: python %USERPROFILE%\.workbuddy\skills\wb-migrate\scripts\wb_migrate.py scan

### 16. 免手动自动恢复（v3.3.0 · Windows 推荐）

不再需要「手动退出 → 手动双击 bat」。注册看门狗后随手退出 WorkBuddy 即可：

```bash
# 注册看门狗：立刻返回；你随后随时退出 WorkBuddy，退出后自动恢复 + 自动重开
python ~/.workbuddy/skills/wb-migrate/scripts/wb_auto.py restore <backup.tar.gz> --relaunch

# 已经退出 WorkBuddy，立即执行
python .../wb_auto.py restore <backup.tar.gz> --now

# 查看/取消看门狗
python .../wb_auto.py status
python .../wb_auto.py cancel
```

看门狗链路：计划任务（Task Scheduler 持有进程，不随终端/会话退出）→ 轮询 `WorkBuddy.exe`
→ 消失后等 `--settle`（默认 8s）释放文件句柄 → `restore --auto-map`
→ 合并专家插件注册表 → 重映射自定义专家目录 → 可选重新拉起 WorkBuddy → 自删计划任务。

常用参数：`--modules`（默认 `conversations,memory,tasks,automations,experts`）、
`--target-user-id`、`--conflict`、`--timeout`（等退出上限，默认 3600s）、
`--no-registry`、`--registry-policy merge|overwrite`。

## Windows 实战踩坑（必读）

### 1. 专家「没迁移」的根因（v3.3.0 已修）
5.9 起专家实体**不在** `~/.workbuddy/experts/`（那里基本是空壳），真实位置是：

```
~/.workbuddy/plugins/cache/experts/<slug>/<version>/     # 专家包本体 agents/ avatars/ SKILL.md
~/.workbuddy/plugins/installed_plugins.json              # 安装记录（plugins 字典）
~/.workbuddy/plugins/marketplaces/experts|my-experts/    # 市场目录
~/.workbuddy/experts/custom/<uid>/                       # 自定义专家，按账号分目录
```

而 `plugins` 在 `ALWAYS_EXCLUDE_TOP_DIRS` 里。v3.2 的 experts 模块只写了 `dirs=["experts"]`，
导致备份包里专家恒为「1 文件 / 249B」。v3.3.0 已补齐上述目录与注册表文件
（实测该机 experts 模块由 1 文件/249B → **102 文件 / 58.1MB**）。

**只恢复文件还不够**：注册表没有条目 App 仍不显示专家。`wb_auto.py` 会额外把包内
`plugins/installed_plugins.json` 的 `plugins` 键并入本机（默认 merge，先自动备份
`*.json.wb-bak-<ts>`），并把 `experts/custom/<旧uid>/` 的内容补进目标账号目录。

### 2. 沙箱 / 权限会拦非 ASCII 路径
受限执行环境（被沙箱包裹的 agent shell）里读 `~/.workbuddy` 下含中文的路径会被拦：

```
[权限不足] [Errno 13] Permission denied: ...\tasks\双品牌周度分析-0701\1.json
[sandbox] 命令被沙箱拦截 (file-read)
```

`dangerouslyDisableSandbox`、换 PowerShell 通道**都绕不过**（拦截发生在子进程层）。
做法是先定位、再用 `--exclude` 跳过，备份照常完成：

```powershell
# 定位非 ASCII 路径（元数据读取不受影响）
Get-ChildItem "$HOME\.workbuddy\tasks" -Recurse -File |
  Where-Object { $_.FullName -match '[^\x00-\x7F]' }
```

```bash
# 跳过（--exclude 可多次）
wb-migrate backup -o ~/Desktop --exclude "tasks/双品牌周度分析-0701/*"
```

实测这类文件通常极少（该机全盘仅 2 个）。

### 3. 输出接管道会让脚本抛 UnicodeDecodeError
接 `grep` / `head` 时 Windows 控制台按 GBK 输出，Python 侧会炸：

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd4 in position 464
```

v3.3.0 已在 `main()` 强制 `stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`。
仍建议**重定向到文件再读**，不要接管道：

```bash
export PYTHONIOENCODING=utf-8
wb-migrate restore <pkg> > restore.log 2>&1
```

### 4. 别用 `-q`
`-q` 只留 WARN，会把**逐表明细、文件动作统计、账号映射条数、回滚快照路径**全部吞掉，
事后无法核对迁移结果。核对时一律不加 `-q`。

### 5. 恢复会被 `EXIT_PERMISSION(3)` 拒绝
Windows 下检测到 WorkBuddy 在跑就直接返回 3（恢复入口的占用守卫），因为 `workbuddy.db`
被进程锁住。**退出要连托盘图标一起退**，只关窗口不够。这正是 `wb_auto.py` 存在的理由。

### 6. `schtasks.exe` 可能被安全策略禁用
报 `PROGRAM BLOCKED BY SECURITY POLICY: schtasks.exe` 时，到
「安全中心 → 命令安全 → 程序黑名单」移除 `schtasks.exe`，或退回 `--now` 手动执行。

### 7. 同机「A 账号 → B 账号」到底生效没
- 默认 `merge` 对**已存在的行/文件一律跳过**，同机重放看起来「什么都没发生」——这是正常的
- 真正的账号迁移靠 `--auto-map` 的 `merge_user_ids()`：直接
  `UPDATE sessions SET user_id=目标 WHERE user_id=源 AND id IN (包内 session)`，
  **不依赖 `--conflict overwrite`**，用默认 `merge` 即可，不会覆盖既有数据
- 恢复前自动快照到 `~/.workbuddy/.wb-migrate-history/restore-{id}/`，`rollback --latest` 撤销
- 局限：`memory` 按 `memory/<uid>_memory.md` 分账号存，merge 下旧账号记忆文件不会被当前账号读取；
  要连记忆一起并，需单独做内容级合并

## 模块清单（--modules 可选值）

| 模块 id | 名称 | 包含内容 | 数据库表 |
|---------|------|----------|----------|
| conversations | 对话 | projects/、workspace/、sessions/ | sessions、session_usage、workspaces、workspace_trust_decisions |
| tasks | 任务 | tasks/、plans/、todos/ | — |
| memory | 记忆 | memory/、memery/、storage/user-*/ | — |
| config | 配置 | local_storage/、cli-env/、project-resources/ + settings.json、mcp.json、argv.json、models.json 等 | migration_meta |
| connectors | 连接器 | connectors/（凭据脱敏）+ mcp.json | — |
| experts | 专家 | `experts/`、**`plugins/cache/experts/`**、`plugins/marketplaces/{experts,my-experts}/`、`expert-history.json`、`plugins/installed_plugins.json`、`plugins/configured-plugins.json`、`plugins/known_marketplaces.json`（v3.3.0 补齐） | — |
| automations | 定时任务 | automations/、automation-backups/ | automations、automation_runs、automation_runtime_state、automation_delivery_outbox |
| skills | 技能 | skills/（默认排除自身） | — |
| artifacts | 产物 | blobs/、clipboard-images/、artifact-index/、media-index/、file-history/、changes-*、file-tree-manifests/、shell-snapshots/ | — |
| shared | 分享文件 | cloudstudio-deploy-history/（含 shareLink） | — |
| apps | 应用 | app/、appearance-resources/（--include-large） | buddy_snapshots |
| assistant | 助理 | user-*/（SOUL/IDENTITY/USER/MEMORY/BOOTSTRAP）、inspiration/ + 根级五件套 | — |

始终排除：binaries/、plugins/、logs/、traces/（--include-large 可含）、skills-marketplace/、app/、cache/、security/、`*-wal`/`*-shm` 等；
5.9 起新增的机器态/跨机有害项也一并排除：`desktop_conversation_migrated`（迁移标记，误迁会阻止目标端导入历史会话）、`.migration-history/`、`.workbuddy-sqlite-migrations/`、`epoch-marker-*.json`、`usage-log.json`、`tencent-docs-engine.port` 等；
未归类目录用 `--include-other` 纳入。

## 核心能力

### 环境兼容性
- 支持 WorkBuddy **5.6.0 – 5.9.0**（本机 5.9.0 实测）；更高/更低版本自动降级为 WARN，`< 5.0` 才阻断
- Python 3.9 – 3.13，**仅标准库**，无需任何 pip 依赖
- macOS 12+ / **Windows 10 1809 (build 17763)+ / Windows 11 / Server 2019+** / Linux 4.15+
- Windows 差异处理：长路径自动加 `\\?\` 前缀、非法字符与保留设备名（CON/PRN/AUX…）净化、大小写冲突兜底、
  只读属性提升、符号链接不入库、**运行占用检测并引导退出**、统一 LF 文本写入（CRLF 会破坏 sha256 校验）
- 版本检测三路兜底：`~/.workbuddy/startup-seed-version.json` → 应用 `package.json` → 系统包管理器/Info.plist

### 备份包格式（v3）
- 结构化包：`manifest.json`（版本/模块/表统计/脱敏记录）+ `checksums.sha256`（逐文件 sha256）+ `database/`（SQLite backup API 一致性快照）+ `modules/<id>/`
- 完整性校验链：manifest 摘要 → 清单 → 逐文件 sha256，任何不匹配拒绝恢复（退出码 2）
- 原子写入（.part + rename），中断不产生损坏包
- 兼容读取 v1/v2 旧包（无 manifest 时自动合成，恢复按白名单过滤未知条目）

### 三种冲突策略
| 策略 | 数据库 | 文件 |
|------|--------|------|
| merge（默认） | INSERT OR IGNORE，跳过已存在 | 已存在跳过 |
| overwrite | 覆盖已存在行（旧值入回滚日志） | 覆盖（原文件入回滚快照） |
| rename | 冲突行改 pk 为 `*.imported-{ts}` 导入 | 冲突文件写 `*.imported-{ts}` |

### 回滚
- 每次恢复前自动创建 `~/.workbuddy/.wb-migrate-history/restore-{id}/`（数据库快照 + 被覆盖文件 + rollback.json 操作清单）
- `rollback` 按序倒序撤销，删除前校验 sha256，防误删恢复后的用户改动
- rename 策略下 session 改名会联动 projects/tasks 目录与关联表外键
- 账号切换共用同一套回滚机制：切换前自动备份当前账号 → 失败自动回滚 → 可手工 `rollback --id switch-xxx`

### 同设备多账号切换（accounts / switch）
与跨设备迁移的边界：**跨设备迁移**用 `backup` + `restore`（换机器/重装/换新账号首次导入）；
**同设备多账号切换**用 `accounts` + `switch`（同一台机器、同一份 `~/.workbuddy` 在 A、B 之间来回切，要求双向可恢复）。

触发入口与账号识别：
- 入口：`wb-migrate accounts`（只读清单）→ `wb-migrate switch --to <USER_ID>`
- 识别来源（多源聚合，任一命中即纳入）：
  - 强信号目录：`connectors/<uid>`、`storage/user-<uid>`、`memory/<uid>_memory.md`、`memery/<uid>_memery.md`、`plugin-account-state/<uid>.json`、`<uid>/`
  - 弱信号目录：`tasks/<uid>`、`todos/`、`plans/`、`experts/`、`local_storage/`（仅佐证，不做归属判定）
  - 数据库：`sessions.user_id` 分组（含会话数与最近活跃）
  - 隔离区：`.wb-migrate-accounts/<uid>/account.json`（已切走的账号在活动位置可能已无痕迹，仍必须可切回）
- 当前账号判定优先级：`--from` 显式指定 > 上次切换写入的 `state=active` 记录 > 综合活跃时间（DB 最近会话 + 专属文件 mtime）

隔离区结构（`~/.workbuddy/.wb-migrate-accounts/<uid>/`）：
```
<uid>/
├── account.json         # 账号元信息: user_id / state(active|stashed) / 切换历史
├── data/                # 账号专属数据切片（切走时移入, 切回时还原）
│   ├── connectors/<uid>/
│   ├── memory/<uid>_memory.md
│   └── storage/user-<uid>/
├── shared/              # 切走时的全局配置快照 (settings.json / SOUL.md / mcp.json ...)
└── db-rows.json         # --db-mode stash 时抽取的数据库行
```
- 同一时刻只有一份数据在活动位置，另一份在隔离区，**不会互相覆盖或污染**
- 切片陈旧副本处理：切回时以活动位置为准，下次切走重新切片覆盖；切换**整体成功后**才清空目标切片
- 同名配置冲突：`keep` 保留当前值 / `overwrite` 用目标账号快照覆盖 / `merge` JSON 浅合并（目标账号值优先，当前独有键保留）；非 JSON 走 `*.imported-{ts}` 副本
- 切换前自动备份当前账号 → 任一步骤抛异常即自动回滚（可用 `--no-auto-rollback` 关闭）
- 每次切换产出 `switch-report.json`：`from`/`to`/策略/各项计数/`errors[]`（含 phase 与失败原因）
- macOS 与 Windows 共用同一套跨平台层：路径分隔符、`\\?\` 长路径、保留名净化、大小写不敏感兜底、统一 LF、运行占用检测（Windows 检测到运行即阻断）

### 脱敏（默认开启）
- Token/Secret/API Key/envelope 等敏感键 → `***WB-REDACTED***`（保留 JSON 结构）
- `.master.key`、`keyblob`、`device-id`、`login-device-nonce` 整文件省略
- mcp.json 的 URL query（apikey 等）与 headers 脱敏
- `--no-redact` 关闭（包内含明文凭据，仅自用迁移）
- 脱敏包恢复后连接器需在目标端重新授权

### 数据库合并（10 表 + 跳过 drizzle）
- 真实 INSERT OR IGNORE + 列交集插入（跨版本 schema 兼容：源多列丢弃、目标独有 NOT NULL 列填类型零值）
- 目标缺表默认 WARN 跳过，`--create-missing-tables` 自动按源 DDL 建表
- `__workbuddy_drizzle_migrations` 永不合入（避免破坏目标 App 迁移状态）
- edge-sync-mapping-v4.db 作为独立文件复制，不合并

### 进度 / 日志 / 中断
- 控制台分阶段进度 + `~/.workbuddy/logs/wb-migrate-{ts}.log` 全量日志
- SIGINT 安全停止；备份可 `--resume` 续传，恢复可用 `rollback` 撤销
- 退出码：0 成功 / 1 错误 / 2 校验失败 / 3 权限不足 / 4 中断

## 典型工作流

### 同设备多账号切换（推荐）
```bash
wb-migrate accounts                                  # 看清本机有哪些账号、谁是活跃的
wb-migrate switch --to <USER_ID> --dry-run           # 先预览
wb-migrate switch --to <USER_ID>                     # 正式切换（自动备份 + 失败自动回滚）
# 切回原账号
wb-migrate switch --to <原USER_ID>
```

### 同机换成全新账号（本机从未登录过）
```bash
wb-migrate backup --output ~/Desktop
# 切换账号（建议新账号先创建一次任意会话）
wb-migrate restore ~/Desktop/wb-backup-*.tar.gz --auto-map
```

### 跨设备迁移（macOS → Windows）
```bash
# macOS: wb-migrate backup --output ~/Desktop
# 传输后 Windows: wb-migrate.bat restore D:\backup.tar.gz --auto-map
```

### 仅迁移技能与记忆
```bash
wb-migrate backup --modules skills,memory -o ~/Desktop
wb-migrate restore <pkg> --modules skills,memory
```

### 恢复 / 切换出错后撤销
```bash
wb-migrate rollback --latest     # 撤销最近一次恢复或切换
wb-migrate rollback --list       # 查看可回滚记录
```

## 限制与注意

- 备份前建议关闭 WorkBuddy，避免数据库写入竞争（工具已用 SQLite backup API 规避 WAL 遗漏）
- **账号切换前必须退出 WorkBuddy**：Windows 下检测到仍在运行会直接阻断（退出码 3），
  macOS/Linux 会告警但继续执行（POSIX 允许移动已打开文件，但缓存数据可能未落盘）
- 目标账号必须是 `accounts` 能列出的账号；本机从未登录过的全新账号请用 `backup` + `restore --auto-map`
- 沙箱模式下执行会弹命令确认框，点击「允许」即可
- 全局参数（--wb-home/-v/-q）必须放在子命令之前；`WB_MIGRATE_HOME` 环境变量可指定数据目录
- 需要 Python 3.9+，仅标准库依赖
