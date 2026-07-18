# Skills Engine

独立的 skills 解析、路由和执行引擎。兼容 Claude Code Agent Skills 开放标准核心子集，提供 CLI 和 Web UI 双界面。

## 特性

- **独立于任何 AI 产品** — 不依赖 Claude Code、Cursor 等
- **兼容 CC 生态** — 能加载和运行 `.agents` 仓库中的 skill
- **四种运行模式** — Steps DSL（确定性）/ 档位 B（tool_dispatch LLM 循环）/ 档位 A（单次 LLM）/ 纯编译（pipe）
- **三级路由匹配** — 精确名称 / 关键词打分（jiebai + intention 权重）/ LLM 兜底
- **安全审批系统** — 离线扫描 + 运行时审批，strict/permissive/off 三种模式
- **会话级审批缓存** — y(本次)/Y(会话允许)/N(拒绝)/r(会话拒绝)/A(全部允许)
- **Web UI** — Gradio 界面，含逐步交互审批
- **Windows 原生支持** — WriteConsoleW 控制台交互，UTF-8 编码

## 安装

```bash
# 基础安装
uv sync

# 安装 Web UI
uv sync --extra ui

# 安装 tokenize（中文分词）
uv sync --extra tokenize
```

## 快速开始

```bash
# 列出 skills
skill-engine list

# 匹配 skills
skill-engine match "生成题解"

# 执行 skill（档位 A：单次 LLM）
skill-engine run "生成题解" --llm

# 执行 skill（Steps DSL 确定性执行）
skill-engine run "生成题解" --steps

# 安全扫描
skill-engine scan-security
```

## 目录结构

```
src/skill_engine/
├── cli.py                # CLI 入口（typer）
├── ui.py                 # Gradio Web UI
├── config.py             # LLM 配置 + 安全配置
├── models/
│   └── models.py         # Skill / SkillMeta / MatchPlan / Step / MatchResult 等
├── routing/
│   ├── router.py         # 三步路由（精确→关键词→LLM）
│   ├── registry.py       # Skill 注册表 + meta 缓存
│   ├── discovery.py      # 多根目录 skill 扫描
│   ├── scoring.py        # 关键词评分（intention/synonym/verb/noun）
│   ├── tokenize.py       # jieba 分词 + 专名提取
│   └── domain_words.py   # jieba 领域词自动注册
├── execution/
│   ├── runner.py         # 核心执行器（四路分流 + 审批）
│   ├── executor.py       # 命令执行（subprocess 沙箱）
│   ├── assembler.py      # Prompt 编译 + !cmd 预处理
│   └── orchestrator.py   # 多 skill 编排
├── security/
│   └── scanner.py        # 离线扫描 + 运行时审批
└── creator/
    ├── creator.py        # Skill 创建
    ├── designer.py       # Skill 设计（LLM prompt）
    ├── preprocessor.py   # Meta 增量抽取
    └── builtins.py       # 内置脚本模板
```

## 路由匹配（三步管线）

```
用户输入 "帮我清理临时文件"
        │
        ▼
 ① 精确匹配 (name/alias/shortcut)
    │
    ▼
 ② 关键词打分 (jieba + intention 权重)
    │  verb 命中: (交集/总数) × 0.5
    │  noun 命中: (交集/总数) × 0.25
    │  phrase bonus: 多字短语 +0.08~0.48
    │  link bonus: 动名双中 +0.15~0.25
    │  0.5 gap 裁断
    ▼
 ③ LLM 兜底 (0 命中/多候选/低分时触发)
    │
    ▼
 返回 MatchPlan (single/multi)
```

## 执行流程（四路分流）

```
runner.run()
   │
   ├─ ① 自动解析 Steps（body 中有 ## Steps？）
   │
   ├─ ② tool_dispatch 循环（LLM 驱动 bash/read/write 工具）
   │
   ├─ ③ 单次 LLM 调用（档位 A）
   │
   └─ ④ 纯编译（返回 final prompt，pipe 给外部）
```

## 安全审批

### 两层架构

- **离线扫描**：正则 + LLM 分析 skill 安全性，只提醒不阻止
- **运行时审批**：`should_approve()` → `_check_approval()` 弹窗交互

### 安全模式

| 模式 | 环境变量 | tool_dispatch | step_exec |
|------|---------|---------------|-----------|
| strict | 不设（默认） | BLOCK（直接拦截） | 危险命令弹窗 |
| permissive | `SKILLS_ENGINE_SECURITY_MODE=permissive` | 命令级检查（安全命令放行） | 危险命令弹窗 |
| off | `SKILLS_ENGINE_SECURITY_MODE=off` | SAFE（全放行） | SAFE（全放行） |

### 审批交互（CLI）

```
⚠️  [cleanup-temp] 请求执行:
   命令: rm -rf temp/cache
   本次允许(y) / 会话允许(Y) / 拒绝(N) / 会话拒绝(r) / 全部允许(A):
```

- `y` — 本次允许
- `Y` — 同命令会话允许（记入 `_session_approvals`）
- `N` — 拒绝本次
- `r` — 同命令会话拒绝
- `A` — 当前会话剩余全部允许

### 危险命令名单

```python
RISKY_BINARIES = {"rm", "cp", "mv", "chmod", "chown", "dd", "mkfs", "python"}
```

## CLI 命令

```bash
# 路由匹配
skill-engine match "查询" [--explain]

# 执行
skill-engine run "查询" [--llm] [--steps] [--dry-run] [--args "参数"]

# 管理
skill-engine list [-v]
skill-engine info <name>
skill-engine install <url|path>
skill-engine update <name>
skill-engine uninstall <name>

# 安全
skill-engine scan-security [name]

# 缓存
skill-engine clear-cache

# Web UI
skill-engine web
```

## SKILL.md 格式

```yaml
---
name: skill-name
description: 技能描述
when_to_use: 触发条件
groups: [category]
---

## User Request

$ARGUMENTS

## Steps

- name: step_name
  type: exec
  command: python scripts/xxx.py $0
  timeout: 30

- name: step_name
  type: llm
  template: LLM 模板，支持 {step_name} 和 $ARGUMENTS

- name: step_name
  type: write
  output_file: output/result.md
  template: 写入内容
```

## Web UI

启动：`skill-engine web`（默认端口 7860）

四个执行面板：

| 面板 | 功能 |
|------|------|
| Skill 列表 | 展示所有可用 skills（含分组） |
| Skill 匹配 | 输入查询展示匹配结果 |
| 直接执行 | 自动匹配 skill 并执行（支持逐步审批） |
| 手动执行 | 选择 skill + 参数（支持逐步交互审批） |

审批流：点击「扫描审批」→ 逐条显示待批命令 → y/Y/N/r 按钮 → 全部完成 →「执行」

## 测试

```bash
# 运行指定模块测试（推荐，避免 Windows tempfile 问题）
uv run pytest tests/test_phase3.py tests/test_phase4.py tests/test_phase8.py -v

# 全量测试
uv run pytest tests/ -q
```

### 已知问题

- Windows 上 `OSError: [Errno 9] Bad file descriptor` — pytest tempfile 清理问题，不影响功能
- `test_integration.py` 部分测试仍传旧 `method=` 参数

## 环境变量

```ini
# LLM 配置（至少配一个）
SENSENOVA_MODEL=longcat-2.0-preview
SENSENOVA_BASE_URL=https://api.example.com/v1
SENSENOVA_API_KEY=sk-xxx

# 安全模式（可选）
# SKILLS_ENGINE_SECURITY_MODE=permissive
# SKILLS_ENGINE_SECURITY_MODE=off

# 自动审批（可选，跳过弹窗）
# SKILLS_ENGINE_AUTO_APPROVE=all
```

## License

MIT