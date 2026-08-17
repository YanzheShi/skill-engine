# MOA（Mixture of Agents）多模型 / 多 skill 协作 —— 设计与落地

> 状态：设计稿 + 已落地代码（2026-08-17）
> 模块：`src/skill_engine/execution/moa.py` / `cli.py:moa` / `models/models.py:MoaAgent` / `config.py` 多模型 profile / `skills/moa-commander/SKILL.md`
> 前置：本文延续「`ToolDispatchRunner.run(skill, llm)` 是唯一执行核心」的架构纪律（见 `docs/多轮会话模式-session-repl设计.md`）。

---

## 0. TL;DR

在保持「单一执行核心」不变的前提下，MOA 在引擎之上加了一层**指挥官驱动的编排层**：把 N 个
`(模型 profile × skill × 任务指示)` 组合成一组 **worker agent**，再由一个 **commander agent** 在每一轮
决定「下一个该谁上、干什么」，形成协作回路。

- **两种用户场景在代码层面完全统一**，区别只在配置（worker 的 `model_profile` / `skill_name` 组合）。
  - 模式 A（多 skill 协作）：A1/A2/A3 各挂不同 skill（如 VLM 审查 + 代码开发）。
  - 模式 B（多模型同 skill 互审）：A1/A2/A3 挂同一 skill、不同模型，互相讨论监督。
- **不用子进程**：编排是串行的（每轮只派一个 agent），复用进程内的 `Executor` / `FileSnapshot` /
  `FileStateTracker` / 黑板，零 IPC、可整轮回滚、文件状态跨 agent 可见。
- **四道防死循环闸**：`max_rounds` / `max_agent_iterations` / `max_llm_calls` / 反震荡强制停止；
  commander 决策解析失败**默认 STOP**（fail-safe）。

---

## 1. 背景与需求

用户的原始诉求（优化一个页面为例）：

1. **多 skill 协作（模式 A）**：前端实现由不同 skill 分工 —— 一个用 VLM model 检查前端实现，另一个用
   代码开发 model 写代码。即「开发这两个 skill 是协作模式」。
2. **多模型同 skill 互审（模式 B）**：多个 model 使用同一个 skill，相互讨论和监督。
3. **交互是「强化的 session」**：新增 `moa` 命令，进入引导，让用户为每个组合选「模型 → skill → 任务指示」，
   起别名 A1~A3；再选一个指挥官（C）的模型 / skill / 指示；确认后由指挥官逐轮驱动。
4. **避免死循环**：限制循环轮次与开销上限。
5. **交互符合现代 CLI 逻辑**（参考 Hermes），思维深度与架构质感要到位。

### 1.1 与既有架构的契合点

skill-engine 的执行核心是 `ToolDispatchRunner.run(skill, llm, snapshot, file_tracker, session_mode)`：
**一个 skill + 一个模型实例，串行 agent loop**。MOA 不需要再造一个执行核心 —— 它只是把多个
`(model_profile, skill, instruction)` 串起来，每次派活都复用 `run()`。这正是「执行核心唯一性」纪律的延伸
（见 `docs/多轮会话模式-session-repl设计.md` §架构设计纪律）。

---

## 2. 架构设计

### 2.1 在引擎中的位置

```
                          ┌─────────────────────────────────────────┐
                          │              CLI 层  `moa` 命令            │
                          │  向导（选模型/选skill/填指示/确认）         │
                          │  + 非交互 --plan JSON 加载                 │
                          └───────────────┬───────────────────────────┘
                                          │ 构造 agents / commander
                                          ▼
                          ┌─────────────────────────────────────────┐
                          │    编排层  MoaOrchestrator（新增）         │
                          │  指挥官决策循环（commander 驱动）          │
                          │  · 解析 <moa_decision> JSON               │
                          │  · 派 worker 执行                          │
                          │  · 写入黑板 / 防死循环四道闸               │
                          │  · 最终综合                               │
                          └───────┬───────────────────┬──────────────┘
                                  │                   │
              ┌───────────────────┘                   └───────────────────┐
              ▼                                                            ▼
   ┌────────────────────────────┐                      ┌────────────────────────────┐
   │  commander.llm (模型 profile)│                     │  worker.llm (模型 profile)   │
   │  + 可选 commander skill      │                     │  + worker skill              │
   └──────────────┬───────────────┘                    └──────────────┬───────────────┘
                  │ 决策                                            │ 执行
                  ▼                                                ▼
          ┌──────────────────────────────────────────────────────────────┐
          │   唯一执行核心  ToolDispatchRunner.run(skill, llm, ...)        │
          │   （共享 Executor / FileSnapshot / FileStateTracker）          │
          └──────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                          ┌─────────────────────────────────────────┐
                          │  共享状态  MoaSession                      │
                          │  snapshot / file_tracker / blackboard     │
                          │  counter / 防震荡字段                      │
                          └─────────────────────────────────────────┘
```

要点：**编排层只做「调度」，执行永远落回 `ToolDispatchRunner.run()`**。任何新增协作能力都不应
再立第二个带 tools/snapshot/ctx/execute 的 SessionManager —— 否则审批、快照、压缩会被架空。

### 2.2 两种模式统一

| 维度 | 模式 A（多 skill 协作） | 模式 B（多模型同 skill 互审） |
|---|---|---|
| A1 | `model=X, skill=前端审查(VLM)` | `model=GPT, skill=代码开发` |
| A2 | `model=Y, skill=代码开发` | `model=Claude, skill=代码开发` |
| A3 | `model=Z, skill=测试` | `model=Gemini, skill=代码开发` |
| 编排逻辑 | **完全相同** | **完全相同** |

区别仅在「worker 的 `(model_profile, skill_name)` 组合」，编排器对二者一视同仁。因此实现上只有
一份 `MoaOrchestrator`，无需为两种模式分支。

---

## 3. 数据模型

```python
@dataclass
class MoaAgent:
    alias: str                       # "A1" / "A2" / "A3" / "C"
    model_profile: str               # config.MODEL_PROFILES 的 key
    skill_name: str = ""             # 空 = 内置纯模型 agent
    instruction: str = ""            # 该 agent 的职责 / 本轮任务基调
    role: str = "worker"             # "worker" | "commander"
    llm: object = None               # 运行时填充（CountingLLM 包装），不序列化
```

`MoaSession`（共享状态持有者，与 session 模式的 `SkillSession` 同思路）：

- `snapshot` + `file_tracker`：跨 worker 共享，使 A1 写出的文件 A2 立即可见、整轮可一键回滚。
- `blackboard: list[dict]`：累积每个 worker 的产出（截断后），作为指挥官与下一 agent 的共享上下文。
- `counter` 由 `CountingLLM` 实时累计；防震荡字段（`last_agent_alias` / `same_agent_streak` /
  `prev_blackboard_hash` / `no_progress_streak`）支撑第四道闸。

> **注意（踩坑）**：`blackboard_hash()` 原先对**整块黑板**求哈希，但每轮新增 entry 必然改变整体哈希，
> 导致「连续同 agent 产出相同」永远判不出「无进展」。已改为 `last_entry_hash()` 只对**最新一笔**求指纹，
> 并纳入输出正文内容（而非仅长度），抗碰撞更强。

---

## 4. 交互设计（强化 session 向导）

参考 Hermes 的渐进式、编号菜单 + 多行粘贴（`Esc+Enter` / `:paste` / `:load`）风格。引导流程：

```
$ skill-engine moa
┌──────────────────────────────────────────────────────────┐
│  MOA · 多模型 / 多 skill 协作向导                          │
└──────────────────────────────────────────────────────────┘
  [总任务] 这次 MOA 要解决的原始任务是什么？
  你> 优化登录页，让它既好看又健壮

  ── 配置 Worker A1 ──
  [Worker A1] 选择模型：        ① default  ② secondary
  你> 1
  [Worker A1] 选择 skill：      ① 内置(无 skill)  ② moa-vlm-check  ③ code-builder
  你> 2
  [Worker A1] 该模型+skill 要做什么？（可 :paste 多行）
  你> 检查登录页前端的视觉与可访问性实现

  继续添加下一个 Worker（A2）？  ① 继续  ② 完成配置，进入指挥官
  你> 1
  …（A2、A3 同理）…

  ── 配置指挥官（Commander） ──
  [指挥官] 选择模型 / skill / 指挥策略
  …（推荐 skill=moa-commander）…

  ┌──── 配置摘要 ────┐
  │ 总任务: 优化登录页 …
  │ A1 default × moa-vlm-check
  │ A2 secondary × code-builder
  │ C  default × moa-commander
  │ 上限: 8 轮 / 60 次 LLM 调用 / 单 worker 12 迭代
  └──────────────────┘
  确认开始任务？  ① 开始(y)  ② 重新配置(r)  ③ 退出(e)
  你> 1
```

向导内每条指示支持 `:paste`（多行落盘成引用 token）与 `:load <文件>`，沿用 session 模式的
`human_io` 输入通道，避免大段粘贴被终端拆行（详见 `docs/多轮会话模式-session-repl设计.md`）。

### 4.1 非交互模式

```bash
skill-engine moa --plan plan.json      # CI / 脚本
skill-engine moa --list-models         # 查看可用模型 profile
skill-engine moa --max-rounds 6 --max-llm-calls 40 -w /path/to/project
```

`plan.json` 结构：`{ "query": "...", "agents": [...], "commander": {...}, "options": {...} }`。

---

## 5. 子进程成本分析（为什么不用子进程）

| 维度 | 子进程方案 | 进程内复用（已采用） |
|---|---|---|
| 冷启动 | 每次派活 fork/启动 Python + 加载模块 | 无，常驻 |
| 上下文传输 | 需把 skill 正文 / 黑板 / 文件状态序列化传过去再传回 | 进程内直接引用，零序列化 |
| 文件可见性 | 需共享文件系统 + 进程间同步 | `FileSnapshot`/`FileStateTracker` 天然共享 |
| 整轮回滚 | 跨进程快照协调困难 | `snapshot.restore()` 一次回滚 |
| 并行收益 | 看似可并行，但 **commander 驱动是串行的**（每轮只派一个） | 串行场景下无并行收益，徒增成本 |
| 成本可观测 | 各进程独立计数，难统一上限 | `CountingLLM` 全局单计数器 |

**结论**：在「指挥官串行驱动」的前提下，子进程只增加成本、不增加吞吐。因此采用进程内复用，
黑板作为共享上下文在内存中传递，零 IPC。若未来需要并行 worker（如多分支探索后由 commander 择优），
再引入隔离沙箱 + 消息总线即可，编排层接口不变。

---

## 6. 防死循环 —— 四道闸

```
① max_rounds (默认 8)        指挥官决策轮数硬上限
        │ 仍续 →
② max_agent_iterations (默认 12)   单 worker 内层 tool_dispatch 迭代上限
        │ 仍续 →
③ max_llm_calls (默认 60)    全局 LLM 调用上限（CountingLLM 实时累计 counter[0]）
        │ 仍续 →
④ 反震荡强制停止    同一 agent 连续命中 max_consecutive_same_agent(默认 3)
                   次 且 黑板无新进展（last_entry_hash 不变）→ 强制 STOP
        │
   任一闸触发 → 跳出循环 → 最终综合
```

### 6.1 关键实现细节

- **闸 #3 用实时计数器**：`CountingLLM` 透明包装任意 LangChain chat model，每次 `invoke` /
  `bind_tools` 后调用都自增 `counter[0]`（可变列表，跨包装共享）。主循环**每轮开头**判断
  `counter[0] >= max_llm_calls`，并在未真正派活前把 `session.round` 记为 `round_no-1`（已完成的轮数）。
- **闸 #4 用增量指纹**：`last_entry_hash()` 只对最新一笔产出求哈希；同一 agent 连续 3 轮且指纹不变
  即判「无进展」强制停，避免 commander 在两个 agent 间无限乒乓或单 agent 空转。
- **fail-safe**：`_parse_decision` 在解析失败 / 非法代号 / 命中 STOP 关键词时**一律返回 STOP**，
  绝不因解析异常继续空转。

### 6.2 终止原因枚举（`stopped_by`）

`commander_stop`（指挥官主动结束） / `max_rounds` / `max_llm_calls` / `anti_loop_forced_stop`
/ `commander_error` / `model_config_error` / `no_agents` / `duplicate_alias` /
`commander_invalid_agent`。最终报告含 `output / rounds / llm_calls / stopped_by / files_created / last_rationale`
（`last_rationale` 记录最近一次指挥官决策的 `rationale`，便于事后追溯为何停止）。

### 6.3 单点失败隔离（worker 异常不拖垮整轮）

`_run_agent` 外包了一层 `_run_agent_safe`：worker 运行期任意异常（工具执行崩溃、模型 5xx 等）
**被隔离在单个 agent 内**，不会向上传播中断整轮 MOA。异常时记录一条**稳定指纹**的错误产出
（含 agent 代号与异常类型，如 `[ERROR] worker A1 执行失败: RuntimeError`），使反震荡闸能识别
「连续失败」而非空转；控制权交还指挥官，由它决定重试 / 换人 / STOP。最终综合阶段若指挥官崩溃，
`_final_synthesis` 同样 fail-safe 回退到拼接黑板并附说明，绝不抛出。

### 6.4 可观测性

`MoaOrchestrator(verbose=True)` 时挂一次性 `StreamHandler` 把内部诊断（决策解析、闸触发、worker
起止）打到 stderr（前缀 `[moa-debug]`），与 `_emit` 的用户进度通道分离；`verbose=False` 时零噪音。

---

## 7. 集成点

### 7.1 多模型配置（`config.py`）
原 `config.py` 只有 `default` / `secondary` 两个 alias，无法「选配置的模型」。已扩展：
- `_build_model_profiles()`：聚合 `LLM_CONFIGS` 与 `SKILL_ENGINE_MODELS` 环境变量声明的 profile。
- `MODEL_PROFILES`（模块级聚合）、`list_model_profiles()`（api_key 脱敏为 `***`）、
  `get_llm_by_profile(profile_name)`（未知 profile 抛 `ValueError`）。
- `MoaOrchestrator` 在运行期为每个 agent 调 `get_llm_by_profile`，并统一包一层 `CountingLLM`。

### 7.2 指挥官 skill（`skills/moa-commander/SKILL.md`）
内置示例 skill，`user_invocable: false`，`groups: [moa, orchestration]`。规定：
- 决策原则（补短板 / 避免重复 / 渐进收敛 / 终止门禁）。
- 输出 `<moa_decision>{"next": "...", "task": "...", "rationale": "..."}</moa_decision>` 围栏，
  与 `MoaOrchestrator._commander_prompt` / `_parse_decision` 的解析契约严格对应。

> 指挥官**也可不选 skill**（纯决策大脑），此时编排器把原始任务 + 名册 + 黑板直接喂给指挥官模型。

### 7.3 注册表（Registry）
worker / commander 的 skill 通过既有的 `discovery.discover` + `Registry.load_skill` 加载，
与单 skill 执行路径完全一致，无新加载机制。

### 7.4 CLI（`cli.py:moa`）
typer 命令，含 `--plan` / `--list-models` / `--max-rounds` / `--max-iter` / `--max-llm-calls` /
`--working-root` / `--verbose`。向导阶段用 `CliHumanIO` 做输入通道，执行阶段 `human_io=None`（进度经
`print`/`emit` 输出，交互已在向导完成）。

---

## 8. 扩展点

1. **并行 worker 探索**：在编排层引入「分叉-汇聚」，多个 worker 并行产出后由 commander 择优合并；
   接口（`agents/commander/run`）不变，仅 `_run_agent` 改并发调度。
2. **黑板分级**：当前黑板是全量拼接（截断），可加「结构化字段」（如 `decisions` / `open_issues`）
   让 commander 检索更精准。
3. **成本预算以 token 计**：`CountingLLM` 现只计调用次数，可扩展为累计 token 数，配合模型单价做预算闸。
4. **持久化会话**：复用 `SkillSession` 思路，把 `MoaSession` 落盘，支持中断后续跑（resume）。
5. **动态增删 worker**：允许 commander 在决策中建议「新增一个专项 agent」，编排层临时实例化。

---

## 9. 验收与测试

`tests/test_moa.py`（12 项，无需真实 LLM / API，用 `ScriptedLLM` 脚本化响应）：
- 多模型 profile 解析与脱敏、未知 profile 报错。
- 决策解析 fail-safe（STOP / 大小写代号 / 非法代号 / 不可解析 → STOP）。
- 指挥官 STOP 提前终止（worker 未被调用）。
- worker 经 `ToolDispatchRunner` 执行并写入黑板、最终综合包含产出。
- 防死循环四道闸：反震荡强制停止（rounds==3）、`max_rounds` 上限、说明 `max_llm_calls` 上限（rounds==1）。
- `CountingLLM` 计数（`invoke` + `bind_tools` 都计入）。

覆盖说明：测试通过脚本化响应验证编排逻辑与四道闸；真实多模型对话质量依赖所选模型与 skill 的实际能力，
不在单测范围内。

---

## 10. 运行方式

```bash
# 交互向导（在目标项目目录内运行，cwd/skills 会被发现）
cd /path/to/project
skill-engine moa

# 非交互：从 JSON 加载配置
skill-engine moa --plan moa_plan.json

# 查看可用模型
skill-engine moa --list-models

# 收紧防死循环上限
skill-engine moa --max-rounds 6 --max-llm-calls 40 -w /path/to/project
```

最小 `plan.json` 示例：

```json
{
  "query": "优化登录页",
  "agents": [
    {"alias": "A1", "model_profile": "default",  "skill_name": "moa-vlm-check", "instruction": "检查前端视觉实现"},
    {"alias": "A2", "model_profile": "secondary","skill_name": "code-builder",  "instruction": "实现/修复代码"}
  ],
  "commander": {"alias": "C", "model_profile": "default", "skill_name": "moa-commander", "instruction": "达到质量门禁即 STOP"},
  "options": {"max_rounds": 8, "max_agent_iterations": 12, "max_llm_calls": 60}
}
```
