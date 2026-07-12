# Skills Engine

独立的 skills 解析和路由工具，兼容 Claude Code Agent Skills 开放标准的核心子集。

## 特性

- **独立于任何 AI 产品** — 不依赖 Claude Code、Cursor 等
- **兼容 CC 生态** — 能加载和运行 `.agents` 仓库中的 skill
- **三种运行模式** — Steps DSL（确定性）/ 档位 A（单次 LLM）/ 纯编译（pipe）
- **命令执行沙箱** — 白名单 + 超时 + PATH/HOME 限制
- **Web UI** — Gradio 界面，4 个面板

## 安装

```bash
# 基础安装
uv sync

# 安装 Web UI
uv sync --extra ui

# 安装 embedding 语义匹配
uv sync --extra embedding

# 安装全部
uv sync --all-extras
```

## 快速开始

```bash
# 列出 skills
skill-engine list

# 匹配 skills
skill-engine match "生成题解"

# 执行 skill
skill-engine run "生成题解" --llm
```

## 架构

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  discovery  │────▶│  registry   │────▶│   router    │
│  多根扫描    │     │  缓存 + 懒加载 │     │  关键词/名称/语义│
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
                                        MatchResult
                                               │
                    ┌──────────────────────────┼──────────────────────────┐
                    ▼                          ▼                          ▼
              ┌──────────┐            ┌──────────────┐            ┌──────────────┐
              │ 纯编译模式  │            │  档位 A       │            │ Steps DSL    │
              │ pipe 给外部 │            │  单次 LLM 调用  │            │ 确定性执行    │
              └──────────┘            └──────────────┘            └───────┬──────┘
                                                                         ▼
                                                                   ┌──────────┐
                                                                   │ executor │
                                                                   │ 沙箱执行  │
                                                                   └──────────┘
```

## 模块

| 模块 | 职责 |
|------|------|
| `discovery.py` | 多根扫描，建立 skill 索引 |
| `registry.py` | Skill 注册表，缓存 + 懒 body |
| `router.py` | 匹配器（名称/关键词/语义） |
| `assembler.py` | 编译器（!cmd 预处理 + 参数替换 + refs） |
| `executor.py` | 命令执行器（沙箱，唯一 spawn 门神） |
| `runner.py` | 执行器（三路分流） |
| `cli.py` | 命令行接口 |
| `ui.py` | Gradio Web UI |

## 测试

```bash
uv run pytest tests/ -v
```

## License

MIT
