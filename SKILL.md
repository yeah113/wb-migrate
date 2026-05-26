---
name: wb-migrate
description: WorkBuddy 数据迁移工具 — 备份、恢复、跨账号/跨设备迁移对话任务、Skills、Automations、配置等所有数据。使用于用户要求备份 WorkBuddy 数据、迁移到新账号/新电脑、恢复任务、导出配置、修复跨平台路径错误、运行 wb-migrate 或处理 WorkBuddy 数据迁移时。支持 macOS/Windows/Linux 跨平台路径自动映射、同系统换账号 user_id 合并、恢复后自动创建工作区目录、自动注册 workspaces 表。
---

# wb-migrate v2.0.3 — WorkBuddy 全量数据迁移

## 触发场景

当用户提到以下需求时，使用此 skill:
- "备份 WorkBuddy 数据"
- "迁移到新账号/新电脑"
- "换账号后恢复任务"
- "导出 WorkBuddy 配置"
- "wb-migrate"、"数据迁移"
- 跨平台迁移时提示路径错误需要修复

## 使用方法

### 1. 扫描当前数据
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py scan
```

### 2. 创建备份
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py backup --output ~/Desktop
```

### 3. 查看备份内容 (含跨平台检测)
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py info <backup.tar.gz>
```
> 自动检测源/目标平台差异并给出建议

### 4. 恢复备份 (智能合并，不覆盖已有数据)
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore <backup.tar.gz>
```

### 5. 预览恢复操作
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore <backup.tar.gz> --dry-run
```

### 6. 跨平台自动映射恢复
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore <backup.tar.gz> --auto-map
```
> 自动：检测源/目标 OS → 映射 sessions.cwd → 合并 user_id → 重命名 projects → 校验

目标账号 user_id 无法自动检测时：
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore <backup.tar.gz> --auto-map --target-user-id <uuid>
```

### 7. 强制覆盖恢复
```bash
python3 ~/.workbuddy/skills/wb-migrate/scripts/wb_migrate.py restore <backup.tar.gz> --force
```

## 核心能力

### 跨平台路径与账号自动映射 (--auto-map)
从旧账号/旧设备恢复到新账号/新设备时，自动处理：
- `sessions.cwd` 路径映射 (`/Users/alice/` ↔ `C:\Users\Alice\`)
- `user_id` 合并 (源账号 → 当前账号)
- `workspaces.path` 更新
- `projects/` 目录名重命名 (匹配新 cwd 编码)
- 缺失工作区目录创建，并恢复基础 `.workbuddy/memory/` 结构
- 按可用 schema 注册 `workspaces` 表，避免跨版本字段差异导致恢复失败
- 空 `~/.workbuddy` 目录首次恢复时同样执行路径映射
- 同系统换账号时也会执行 `user_id` 合并；目标账号未产生任何会话时，用 `--target-user-id` 显式指定

### 恢复后自动校验
每次恢复完成后自动检查：
- sessions-cwd 有效性
- workspace 目录存在性
- projects 目录匹配
- tasks-sessions 关联
- user_id 一致性

### 安全增强
- 恢复前自动备份目标数据库 (`workbuddy.db.backup-{ts}`)
- SQLite 一致性快照备份，避免遗漏 WAL 中未 checkpoint 的数据
- 备份包完整性校验与安全解包，兼容 Python 3.9+
- 问题报告有明确的修复指引

## 备份包含的数据

| 模块 | 路径 | 说明 |
|------|------|------|
| 数据库 | workbuddy.db | Sessions, Automations, Workspaces |
| Skills | skills/ | 用户创建的所有 Skills |
| 项目数据 | projects/ | 对话历史 (jsonl) |
| 连接器 | connectors/ | MCP 连接器配置 |
| 会话状态 | sessions/ | 活跃会话 |
| 本地存储 | local_storage/ | 键值存储 |
| 追踪日志 | traces/ | 执行历史 |
| 任务列表 | tasks/ | 任务管理 |
| 团队配置 | teams/ | 团队结构 |
| 文件历史 | file-history/ | 文件版本 |
| 身份文件 | SOUL.md 等 | 配置与偏好 |

## 智能合并策略

恢复时采用以下策略避免数据覆盖:
- **数据库**: INSERT OR IGNORE — 仅导入目标数据库中不存在的记录
- **Skills**: 按名合并 — 新文件直接添加，已有文件跳过
- **项目数据**: 按目录合并 — 新项目直接添加
- **配置文件**: 仅在目标不存在时创建

## WorkBuddy 数据架构要点

在执行迁移任务时，需要了解以下关键知识：

### 路径编码规则
| 平台 | cwd 示例 | project 目录名 |
|------|---------|---------------|
| Windows | `C:\Users\Alice\WorkBuddy\xxx` | `c-Users-Alice-WorkBuddy-xxx` |
| macOS | `/Users/alice/WorkBuddy/xxx` | `Users-alice-WorkBuddy-xxx` |

### 数据隔离
- **Sessions**: 通过 `user_id` 字段隔离，不同账号不可见
- **Tasks**: 挂在 session ID 下
- **对话数据**: `~/.workbuddy/projects/<encoded-cwd>/<session-id>/`
- **Skills/Projects/Connectors**: 不区分账号，所有用户共享

## 典型工作流

### 同机换账号
```bash
wb-migrate backup --output ~/Desktop
# 切换账号，建议新账号先创建一次任意会话
wb-migrate restore ~/Desktop/wb-backup-*.tar.gz --auto-map
```

### 跨设备迁移 (macOS → Windows)
```bash
# macOS 上
wb-migrate backup --output ~/Desktop

# 传输到 Windows 后
wb-migrate restore D:\backup.tar.gz --auto-map
```

### 修复已导入但路径错误的数据
```bash
# 如果之前没加 --auto-map 导入，重新导入
wb-migrate restore backup.tar.gz --auto-map
```

## 限制与注意

- **沙箱权限**: 在 WorkBuddy Desktop 的「安全沙箱」模式下执行时，会弹出命令确认框，点击「允许」即可正常完成备份/恢复（整条命令执行期间不会再拦截）。也可切换为「完全放开」模式省去确认步骤。
- 二进制 (binaries/)、应用 (app/)、插件 (plugins/) 默认不备份
- 备份前建议关闭 WorkBuddy，避免数据库写入冲突
- 恢复时不覆盖已有数据，使用 `--force` 可强制覆盖
- 跨平台迁移时务必使用 `--auto-map`
