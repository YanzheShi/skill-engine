# Skills Engine

独立的 skills 解析、路由和执行引擎。兼容 Claude Code Agent Skills 开放标准核心子集，提供 CLI 和 Web UI 双界面。

## 特性

- **独立于任何 AI 产品** — 不依赖 Claude Code、Cursor 等，自带 CLI 和 Web UI
- **兼容 CC 生态** — 能加载和运行 `.agents` 仓库中的 skill
- **四种运行模式** — Steps DSL（确定性）/ tool_dispatch LLM 循环（档位 B）/ 单次 LLM（档位 A）/ 纯编译（pipe）
- **三级路由匹配** — 精确名称 / 关键词打分（jieba + intention 权重）/ LLM 兜底
- **安全审批系统** — 离线扫描 + 运行时审批，strict/permissive/off 三种模式
- **会话级审批缓存** — y(本次)/Y(会话允许)/N(拒绝)/r(会话拒绝)/A(全部允许)
- **自动审批** — 支持 `skill_name:binary` 粒度格式
- **MCP 服务器支持** — 通过 `mcp.json` 连接外部 MCP 服务器（stdio/HTTP/SSE）
- **文件快照** — 执行前自动创建文件检查点，支持回滚
- **多轮 REPL 会话** — 同一 skill 的持续交互式会话
- **元数据预处理** — 自动抽取 intention / synonyms / purpose / keywords 增强匹配
- **Skill 创建** — 通过自然语言描述，LLM 自动生成 skill
- **Web UI** — Gradio 界面，含逐步交互审批
- **跨平台路径** — 自动归一化 Git Bash / WSL / Cygwin 路径到 Windows 原生路径
- **Windows 原生支持** — WriteConsoleW 控制台交互，UTF-8 编码

## 安装

本项目用 [uv](https://docs.astral.sh/uv/) 管理依赖（也兼容 pip）。`skill-engine` 是一个带命令行入口（`skill-engine`）的可安装包，要求 **Python >= 3.12**。

### 方式一：用 uv 安装（推荐，开发者）

```bash
# 1. 安装 uv（若未装）
curl -LsSf https://astral.sh/uv/install.sh | sh        # macOS / Linux
# Windows (PowerShell): irm https://astral.sh/uv/install.ps1 | iex

# 2. 克隆并安装（含所有可选依赖）
git clone https://github.com/YanzheShi/skill-engine.git
cd skill-engine
uv sync --all-extras          # 等价于旧文档里的 ui / tokenize 等
```

### 方式二：用 pip 安装（通用）

```bash
pip install -e .              # 可编辑安装（开发用，改代码即时生效）
# 或装成普通包
pip install .
```

### 方式三：一行装到全局 PATH（uv tool，适合纯使用者）

把 `skill-engine` 命令直接装进隔离环境并加入 PATH，装一次到处能用：

```bash
uv tool install git+https://github.com/YanzheShi/skill-engine.git
# 升级
uv tool upgrade skill-engine
```

> 区别：`uv sync` 是**开发态**命令（建 `.venv`、锁依赖、便于改代码）；`uv tool install` / `pip install` 是把包装进隔离或当前环境，更适合"只用命令行"的人。

## 环境变量配置

复制 `.env.example` 为 `.env` 并填入配置：

```bash
cp .env.example .env
```

最小配置只需要一个 LLM 提供商（OpenAI 兼容 API）：

```ini
LLM_MODEL=gpt-4o
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-your-api-key
```

支持任何 OpenAI 兼容的 API 提供商，包括：

- OpenAI（GPT 系列）
- SenseNova（商汤）
- DeepSeek
- 通义千问（DashScope）
- vLLM 本地部署
- Ollama 本地模型
- 其他任何兼容 OpenAI 接口的服务

> 完整配置项见 [.env.example](.env.example)。

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

# 执行 skill（tool_dispatch LLM 循环）
skill-engine run "生成题解" --tool-dispatch

# 安全扫描
skill-engine scan-security

# 启动 Web UI
skill-engine web
```

## 目录结构

```
src/skill_engine/
├── cli.py                # CLI 入口（typer）
├── ui.py                 # Gradio Web UI
├── config.py             # LLM 配置 + 安全配置 + MCP 配置
├── models/
│   └── models.py         # Skill / SkillMeta / MatchPlan / Step / RunResult 等
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
│   ├── steps.py          # Steps DSL 执行器
│   ├── orchestrator.py   # 多 skill 编排
│   ├── tool_dispatch.py  # 档位 B LLM 工具循环
│   ├── tool_defs.py      # 内建工具定义（bash/read/write/search 等）
│   ├── context_manager.py # token 预算 + 历史压缩
│   ├── mcp_client.py     # MCP 服务器连接客户端
│   ├── snapshot.py       # 文件检查点 / 回滚系统
│   ├── human_io.py       # 人机交互抽象层（CLI / prompt_toolkit）
│   ├── paste_buffer.py   # 大段内容外置化保存
│   └── paths.py          # 跨平台路径归一化
├── security/
│   └── scanner.py        # 离线扫描 + 运行时审批
└── creator/
    ├── creator.py        # Skill 创建
    ├── designer.py       # Skill 设计（LLM prompt）
    ├── preprocessor.py   # Meta 增量抽取（intention/synonyms/purpose/keywords）
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

### 档位 B（tool_dispatch）工作流

```
tool_dispatch_loop()
   │
   ├─ 初始化上下文管理器（token 预算）
   ├─ 连接 MCP 服务器（如有配置）
   ├─ 循环：
   │   ├─ LLM 选择工具并生成参数
   │   ├─ 安全审批检查
   │   ├─ 执行工具
   │   ├─ 文件快照（执行前自动备份）
   │   └─ 结果反馈给 LLM
   ├─ 达到 max_iterations 或 LLM 返回最终结果时退出
   └─ 清理 MCP 连接
```

### 内建工具（tool_dispatch 模式可用）

| 工具 | 功能 |
|------|------|
| `bash` | 执行 shell 命令 |
| `read_file` | 读取文件内容 |
| `write_file` | 写入文件 |
| `edit_file` | 编辑文件（行级替换） |
| `search_files` | 搜索文件内容 |
| `web_search` | 网络搜索（需配置 Tavily） |
| `get_current_time` | 获取当前时间 |
| `stop` | 停止执行 |

可通过 `extra_tools` 和 `mcp_servers` 扩展工具集。

## 元数据预处理

`skill-engine index` 命令会对所有 skill 进行 LLM 驱动的元数据抽取：

```bash
# 增量预处理（仅处理新 skill）
skill-engine index

# 全量重建
skill-engine index --rebuild-meta
```

抽取的元数据字段：`intention`、`synonyms`、`purpose`、`keywords`，这些信息会参与路由匹配的第二阶段关键词打分。

## MCP 服务器集成

通过 `mcp.json` 配置文件连接外部 MCP 服务器：

```json
{
  "mcp_servers": {
    "my-server": {
      "command": "node",
      "args": ["server.js"],
      "env": {"KEY": "value"}
    }
  }
}
```

支持 stdio、HTTP、SSE 三种传输方式。

## 多轮 REPL 会话

对同一 skill 发起持续交互式会话：

```bash
skill-engine session "帮我写代码" --skill leetcode-solution-writer --max-iter 10
```

支持状态持久化（`--state-path`）和断点恢复（`--resume-from`）。

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

### 自动审批

通过环境变量 `SKILLS_ENGINE_AUTO_APPROVE` 配置：

```ini
# 全部放行
SKILLS_ENGINE_AUTO_APPROVE=all

# 指定 skill 的指定命令放行
SKILLS_ENGINE_AUTO_APPROVE=cleanup-temp:rm
```

### 危险命令名单

```python
RISKY_BINARIES = {"rm", "cp", "mv", "chmod", "chown", "dd", "mkfs", "python"}
```

## CLI 命令

```bash
# 路由匹配
skill-engine match "查询" [--explain]

# 执行
skill-engine run "查询" [--llm] [--steps] [--tool-dispatch]
                       [--dry-run] [--args "参数"]
                       [--max-iter <N>] [--non-interactive]
                       [--working-root <DIR>] [--state-path <PATH>]
                       [--resume-from <PATH>]

# 多轮会话
skill-engine session "查询" [--skill <NAME>] [--max-iter <N>]
                            [--working-root <DIR>] [--state-path <PATH>]
                            [--resume-from <PATH>]

# 扫描
skill-engine scan [--root DIR]

# 索引（元数据预处理）
skill-engine index [--build-meta] [--rebuild-meta]

# 创建 skill（LLM 驱动）
skill-engine create "生成 vue 组件" [--name <NAME>] [--dry-run]

# 管理
skill-engine list [-v]
skill-engine info <name>
skill-engine install <url|path>
skill-engine update <name>
skill-engine uninstall <name>

# 安全
skill-engine scan-security [name] [--deep] [--json]

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
argument_hint: 参数提示
allowed_tools: [bash, read_file, write_file]
mcp_servers: [my-server]
human_in_loop: true
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

支持更多元数据字段：`alias`、`shortcuts`、`intent_verbs`、`agent`、`model`、`effort`、`context`（INLINE/FORK）、`turn_policy`、`context_budget`、`disabled_model_invocation`、`user_invocable`、`disallowed_tools` 等。

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

完整的环境变量列表见 [.env.example](.env.example)。核心配置项：

```ini
# LLM 配置（至少配一个）
LLM_MODEL=gpt-4o
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-xxx

# 安全模式（可选）
# SKILLS_ENGINE_SECURITY_MODE=permissive
# SKILLS_ENGINE_SECURITY_MODE=off

# 自动审批（可选，跳过弹窗）
# SKILLS_ENGINE_AUTO_APPROVE=all

# MCP 配置（可选）
# SKILL_ENGINE_MCP_CONFIG=./mcp.json

# 搜索 API（可选）
# TAVILY_API_KEY=tvly-xxx
```

## 在其他环境中使用

### 作为库集成到你的 Python 项目

```python
from skill_engine import Router, Runner
from skill_engine.cli import app
```

### 容器化（Docker）

适合 CI、服务器，或"不想配 Python 环境"的场景。最小 `Dockerfile`：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY . .
RUN uv sync --all-extras
ENTRYPOINT ["uv", "run", "skill-engine"]
```

构建运行：

```bash
docker build -t skill-engine .
docker run --rm -v "$PWD/skills:/app/skills" -e LLM_API_KEY=sk-xxx skill-engine list
```

### 打包成单文件可执行程序（PyInstaller）

适合分发给"完全不想装 Python"的人。在仓库根目录执行：

```bash
uv pip install pyinstaller
pyinstaller --name skill-engine --onefile \
  --hidden-import skill_engine.cli \
  --collect-submodules skill_engine \
  src/skill_engine/cli.py
```

产物在 `dist/skill-engine[.exe]`，可直接拷贝到其他同系统机器运行（无需 Python）。

> 注意：`gradio`（Web UI）和 `jieba`（分词）在单文件打包时需额外 `--collect-data`；若只发 CLI，可省去 `--extra ui` 以减小体积。

## 开源发布清单

- [ ] `LICENSE` 已添加（MIT）
- [ ] `README.md` 含安装、快速开始、配置说明
- [ ] `.env.example` 已提交，真实 `.env` 已被 gitignore 忽略（本项目已满足）
- [ ] `.gitignore` 覆盖 venv / 构建产物 / 运行时产物 / 密钥（本项目已满足）
- [ ] `pyproject.toml` 的 `dependencies` 无私有 / 内部依赖
- [ ] 代码无硬编码密钥、内网地址
- [ ] 测试可跑：`uv run pytest tests/ -q`
- [ ] GitHub 仓库设为 **Public**，并填好 About / Topics
- [ ] 打 release tag 并写 GitHub Release Note（如 `git tag v0.1.0 && git push --tags`）
- [ ] （可选）`CONTRIBUTING.md`、`CHANGELOG.md`、CI（GitHub Actions）

## License

MIT