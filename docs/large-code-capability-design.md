# 大型代码处理能力 — 设计与实施文档

> 目标：让 skill-engine 的 code-builder 具备像 Cline / pi-coding-agent 那样处理、修改大型项目的能力，
> 同时**保持它作为通用 skill 执行引擎的定位**。
>
> 本文档沉淀了差距分析、分层纪律、以及已落地的两份 P1 方案（工具可插拔接口 + 上下文管理器）的设计与代码落点。

---

## 1. 背景与核心结论

code-builder skill 本身是"纯 prompt + 档位 B 循环"，四步工作流、计划确认、原子化 `edit_file` 骨架与 Cline 同构。
瓶颈**全在引擎层**：prompt 再好也补不了引擎不给的能力。

差距结论（按严重度）：

| # | 差距 | 严重度 | 现状代码证据 |
|---|------|--------|--------------|
| 1 | 上下文管理 | 致命 | `tool_dispatch.py` 的 `messages` 只增不减，仅单条截 5000 字；大项目必撑爆窗口 |
| 2 | 代码库感知 | 致命 | 无 `list_files`，LLM 看不到目录树只能盲搜；`search_files` 是 rglob 逐行正则，不认 `.gitignore` |
| 3 | 编辑容错 + 回滚 | 重要 | `edit_file` 精确匹配无模糊兜底；写盘无检查点，改 8 个文件第 9 个挂了无法恢复 |
| 4 | 任务持久化 | 重要 | `run()` 纯内存态，max_iterations 耗尽直接丢弃全部进度，无法分段续跑 |
| 5 | 执行环境 | 小 | Executor 默认 10s 超时，`pytest` 全量/构建命令跑不完；Windows 走 cmd.exe |

**关键设计纪律（决定"是否还通用"）**：通用能力沉引擎核心，代码专用能力留 skill 层。
- 沉核心（通用）：上下文管理、bash 超时可配、edit 模糊容错、todo 落盘续跑、快照/回滚（抽象为"快照"，别绑死 git）。
- 留 skill 层（code 专用）：`ast` 签名地图、`git` 检查点、目录树工具。

只要遵守这条纪律，后续加任何领域的 skill（写作、数据分析、运维）都是"注册即用、核心不动"。

---

## 2. 与 Cline 的能力差距全景（P1/P2/P3）

- **P1 保命（≈12.5 人天）**：上下文管理器 + 代码库感知（list_files / ast_map / gitignore 过滤）+ bash 超时可配。中大型项目才算"能跑"。
- **P2 可靠（≈22.5 人天）**：edit 模糊匹配兜底 + shadow git 检查点/回滚 + todo 落盘 + 断点续跑。能扛真实的大重构。
- **P3 进阶（≈34.5 人天）**：子代理探索、tree-sitter AST 工具、Plan/Act 双模式硬隔离。在能力上对齐 Cline。

> 集成测试是被低估的大头（≈6 人天）——改动集中在 `tool_dispatch.py` / `executor.py` / `tool_defs.py` 核心链路，项目有 49 个测试文件，回归风险高。

---

## 3. 方案 A：工具可插拔接口

**目标**：让任意 skill 能自带领域工具，核心引擎不硬编码、不感知具体领域。

### 3.1 数据模型（`models.py`）

`SkillMetadata` 与 `MergedMeta` 各增加一个字段（与现有 `allowed_tools` / `disallowed_tools` 对齐）：

```python
extra_tools: list[str] = Field(
    default_factory=list,
    description="额外工具模块文件名（相对 skill 目录，如 ['tools.py']），引擎自动加载其中 @tool 并合并进 bind_tools",
)
```

### 3.2 工具注册表（`tool_defs.py`）

把写死的列表升级成可扩展注册表 + 加载器：

```python
TOOL_REGISTRY: dict[str, BaseTool] = {t.name: t for t in TOOL_DISPATCH_TOOLS}

def load_skill_tools(skill) -> list[BaseTool]:
    modules = getattr(skill.metadata, "extra_tools", None) or []
    if not modules:
        return []
    loaded: list[BaseTool] = []
    for rel in modules:
        tools_py = Path(skill.directory) / rel
        if not tools_py.exists():
            log.warning("extra_tools 声明的模块不存在: %s", tools_py)
            continue
        # importlib 从绝对路径隔离加载，避免污染全局命名空间
        spec = importlib.util.spec_from_file_location(mod_name, str(tools_py))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        loaded += [v for v in vars(mod).values() if isinstance(v, BaseTool)]
    return loaded
```

### 3.3 绑定合并 + 过滤（`tool_dispatch.py:244`）

```python
if hasattr(llm, "bind_tools"):
    tools = list(TOOL_REGISTRY.values()) + load_skill_tools(skill)
    disallowed = getattr(skill.metadata, "disallowed_tools", None) or []
    allowed = getattr(skill.metadata, "allowed_tools", None) or []
    if disallowed:
        tools = [t for t in tools if t.name not in disallowed]
    if allowed:
        tools = [t for t in tools if t.name in allowed]
    llm_with_tools = llm.bind_tools(tools)
else:
    llm_with_tools = llm
```

### 3.4 ⚠️ 执行分支（关键补充，原方案只覆盖了"绑定"）

`bind_tools` 只负责"让 LLM 知道有这些工具"，但 `tool_dispatch.run()` 的工具执行链（`if/elif tc["type"] == ...`）
**只认识 6 个内建工具名**。自定义 skill 工具（如 `cb_list_files`）若不处理，会落进 `else` 分支变成"未知工具类型"。

因此必须在循环里增加一个**通用执行分支**，按工具名从加载到的 skill 工具表里查找并 `.invoke()`：

```python
elif tc["type"] in skill_tools_map:
    tool_obj = skill_tools_map[tc["type"]]
    prev_cwd = os.getcwd()
    try:
        os.chdir(str(base_dir))          # 与 bash 一致的基准目录
        result = tool_obj.invoke(tc["input"])
    finally:
        os.chdir(prev_cwd)
    content = result if isinstance(result, str) else str(result)
    messages.append({
        "role": "tool", "tool_call_id": tc["id"],
        "name": tc["type"], "content": self._truncate_msg(content),
    })
```

> `skill_tools_map` 在绑定阶段一并算出：`{t.name: t for t in load_skill_tools(skill)}`。

### 3.5 对其他 skill 的影响

无 `extra_tools` 的 skill 走原路径，`TOOL_REGISTRY` 即原 6 个工具，行为零变化。
工具名冲突防护：约定 skill 自带工具加领域前缀（如 `cb_`），避免两 skill 同名工具互相覆盖。

---

## 4. 方案 B：P1 上下文管理器

**目标**：当前 `messages` 从头 append 到尾、从不压缩，大项目读十几个文件必撑爆窗口。

### 4.1 新建模块 `src/skill_engine/execution/context_manager.py`

```python
class ContextManager:
    def __init__(self, budget: int = 8192, keep_recent: int = 4, threshold: float = 0.8):
        self.messages: list[dict] = []
        self.budget = budget            # token 上限
        self.keep_recent = keep_recent  # 保留最近多少个完整轮次
        self.threshold = threshold      # 触发压缩的预算占比

    def estimate_tokens(self) -> int:          # 轻量估算：字符数 // 4，零依赖
    def maybe_compress(self, llm) -> bool:    # 超阈值则摘要压缩旧历史
```

### 4.2 压缩策略（关键决策）

1. **触发**：`estimate_tokens() > budget * 0.8`（默认 8192 的 80% ≈ 6500 token）。
2. **范围**：首条 user prompt（final_prompt）不动；最近 `keep_recent`（默认 4）个完整轮次原样保留；中间历史压缩。
3. **轮次感知**：以"assistant 且带 tool_calls"为轮次起点，避免破坏工具调用配对（不会把半截 tool_call 留下）。
4. **怎么压**：把待压缩段交给 `llm` 生成 `<condensed_history>…</condensed_history>` 摘要，原地替换回 `messages`（保持外部引用有效）。
5. **token 估算**：先用 `字符数 // 4` 近似（零依赖、跨模型够用），预留精确 tiktoken 接口。

### 4.3 集成点（`tool_dispatch.py` 的 `run()`）

- 初始化：`ctx = ContextManager(budget=getattr(skill.metadata, "context_budget", 0) or 8192)`
- 用 `ctx.messages` 替代裸 `messages`：`messages = ctx.messages`；每轮 `resp = llm_with_tools.invoke(messages)` 前调用 `ctx.maybe_compress(llm)`。
- `models.py` 增加 `context_budget: int = 0`（0=用默认 8192）。
- 现有 `_truncate_msg`（单条 5000 字）保留为兜底。

---

## 5. 实施落点文件清单

| 方案 | 新建 / 改动 | 核心改动 |
|------|-------------|----------|
| 工具可插拔接口 | 改 `models.py` / `tool_defs.py` / `tool_dispatch.py` | +`extra_tools` 字段、`TOOL_REGISTRY` + `load_skill_tools()`、bind 合并 + 过滤、**循环内通用执行分支** |
| P1 上下文管理器 | 新 `context_manager.py` / 改 `tool_dispatch.py` / 改 `models.py` | +`ContextManager` 类、`run()` 用其管 messages、+`context_budget` |
| code-builder 示例 | 新 `skills/code-builder/tools.py` + 改 `SKILL.md` frontmatter | `cb_list_files` / `cb_ast_map` / `cb_git_checkpoint`，frontmatter 声明 `extra_tools: ["tools.py"]` |
| **P2-1 edit 模糊兜底** | 改 `tool_dispatch.py` | +`_apply_edits()` / `_fuzzy_find()` / `_norm_ws()` 纯函数：精确优先，oldText 不存在时走行级宽松模糊（去首尾空白 / 归一化空白的行窗口匹配），唯一候选才应用，多候选/全失败回错给 LLM 重试 |
| **P2-2 通用文件快照/回滚** | 新 `snapshot.py` / 改 `tool_dispatch.py` | +`FileSnapshot` 类（`.skill_engine_snapshots/`，仅首次记录"进入前内容"，落盘 manifest）；`write_file`/`edit_file` 写盘前 `record()`；新增内建工具 `restore_file`（不依赖 git） |
| **P2-3 todo 落盘续跑** | 改 `tool_dispatch.py` / `runner.py` | `run()` +`state_path`/`resume_from` 参数；`try/finally` 全退出路径落盘（messages/进度）；`resume_from` 载入历史续跑；`runner.run`/`_run_tool_dispatch` 透传 |

---

## 6. 测试策略

- **单元**：`load_skill_tools` 从临时 skill 目录加载 `@tool`；合并 + `disallowed`/`allowed` 过滤；无 `extra_tools` 返回 `[]`。
- **单元**：`ContextManager.estimate_tokens` / `maybe_compress` 触发阈值 / 轮次感知压缩保留首条与最近轮次。
- **集成**：用带 `bind_tools` 的 Mock LLM 跑 `runner.run`，验证 skill 自带工具能被真正执行（非"未知工具类型"）。
- 不动 `test_phase_tool_dispatch.py`（其从 `runner` 导入 `TOOL_DISPATCH_TOOLS` 的断言属历史遗留，不在本次范围）。

---

## 7. 风险与后续

- **P2 项（已落地，见 §5）**：edit 模糊兜底（核心 `_apply_edits`）、通用文件快照/回滚（`snapshot.py` + 内建 `restore_file`，不绑 git）、todo 落盘续跑（`run()` 的 `state_path`/`resume_from` + `runner` 透传）。三块均守住"通用项沉核心、code 专用留 skill 层"纪律：快照走引擎核心、code-builder 的 `cb_git_checkpoint` 仍作 skill 层可选示范。
- **P3 项（未做）**：子代理探索、tree-sitter AST 工具、Plan/Act 双模式硬隔离——对齐 Cline 的进阶能力，需额外 ~12 人天。
- **外部依赖风险**（仅 P3）：`tree-sitter` 解析、shadow git 边界（嵌套仓库 / 大二进制）。
- **通用性回归**：新增 skill 时务必自检——它用的工具是否通过 `extra_tools` 注入、有无前缀防冲突、是否沉到了 skill 层而非污染核心。
