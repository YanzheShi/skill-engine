# 多轮会话模式 (Session / REPL) 设计文档

> 状态：设计已定稿，待实现。最后决策更新：2026-07-31

## 1. 背景与问题

当前 code-builder 等 skill 的工作流是：

```
skill-engine run "帮我重构模块X" -> ToolDispatchRunner.run() -> 输出结果 -> 进程退出
```

ToolDispatchRunner.run() 是单体循环：一次调用跑完 LLM 调用、工具执行、多轮人机交互，然后返回、进程退出。
之后再想说"现在重构模块Y"，必须重新 run -> 重新 router 匹配、重新编译 prompt、重新消耗 token 理解上下文。

核心症结：外层（用户交互）与内层（工具调用）循环耦合在 run() 内（487-525 行），且进程一退出状态全丢。

## 2. 设计目标

- 会话持久：messages 历史、文件快照、安全审批缓存、MCP 连接跨轮保持
- 零改造现有 skill：SKILL.md 不改，human_in_loop / TurnPolicy 在 run 模式下继续复用
- 退出机制：用户输入 /exit、/done 或 stop 工具优雅退出
- 可中断：Ctrl+C 不丢会话状态（复用 P2-3 resume_from）

## 3. 现状可复用资产（已存在，白送）

- tool_dispatch.py:487-525 内层多轮内核（LLM 出文本 -> 问用户 -> append -> continue）
- HumanIO(ABC) + CliHumanIO（console 实现）、TurnPolicy（should_stop/max_turns/user_exit）、RunResult.history
- ToolDispatch.__init__ 已接收 human_io / turn_policy；runner.py:384 按 human_in_loop 自动接通
- P2-3 _save_state / _load_state + resume_from：messages 落盘/续跑
- MCP 合并逻辑 tool_dispatch.py:357-390：session 复用 run() 自动继承

## 4. 方案对比与结论

| 维度 | 混合方案（采用） | 独立 SessionManager（否决） |
|---|---|---|
| 执行核心 | run() 唯一 | SessionManager 自带 core，第二条 |
| MCP/审批/快照 | 全继承 | 重写会架空 |
| 代码重复 | 低 | 高 |
| skill 零改造 | 是 | 是 |

结论：保留 run() 为唯一执行核心，新增外层 REPL 编排层。

## 5. 核心设计

- 两层解耦：
  - run(session_mode=True, initial_messages=...) = 执行一个子任务的核心：LLM -> 工具 -> ... -> 出文本/stop，不在内部主动追问用户。
  - runner.run_repl() = 编排层：持有 SkillSession.messages，循环驱动 run、读用户输入、判 /exit、每轮落盘。
- 轮边界（主，无需改 SKILL.md）：run(session_mode) 在无 tool_calls 时返回文本 → REPL 打印并提示"下一步指令 / /exit"，用户下一句作为新轮 initial_messages。这天然兼容 code-builder SKILL.md"第二步不要调工具等确认"——LLM 输出计划文本即交回，用户"确认"成为下一轮指令。
  - ask_user 内置工具（增强，轮内暂停）：LLM 需要"特定答案才能继续"时调它，引擎暂停 human_io.read()读输入，作为 tool result 回灌 LLM 继续当前轮（不结束 turn）。与轮边界不同：答案直接驱动后续步骤，而非开启新指令。
  - stop 内置工具：LLM 调它 = 显式结束子任务，run 返回 REPL（stopped_by="tool_stop"）。
  - /exit、/done：用户主动退出。
  - 引导：session system prompt 须明确"例行进度直接输出文本（轮边界）；需用户决策才能继续时调 ask_user"，避免 LLM 滥用 ask_user 导致每步卡顿。
- initial_messages：续轮传入历史 messages，跳过重建 final_prompt。
- 薄 SkillSession 数据对象：持有 messages / snapshot / working_root / state_path。
- 持久化：复用 resume_from + 每轮 _save_state。

## 6. 详细实现（逐文件骨架）

### tool_dispatch.py
- run() 新增参数 initial_messages: list|None = None、session_mode: bool = False
- session_mode=True 时：跳过 487-525 的内部 human_io 追问循环，仅执行内层 _process_until_text 后返回 RunResult
- 返回标记：session_mode 下无 tool_calls 出口填 stopped_by="session_turn_end"，供 REPL 与 tool_stop 统一识别为"等下条指令"。
- initial_messages 非空时跳过 final_prompt 重建，直接以历史起轮
- 提取 _process_until_text(messages) 为可重入方法（可选重构，提升可测性）

### tool_defs.py
- 新增 ask_user / stop 两个内置工具（继承 BaseTool）
  - ask_user：execute 内调 self.human_io.read() 返回用户输入
  - stop：返回 stop 信号，run 据此结束本轮

### runner.py
- 新增 run_repl(query, skill_name=None, working_root=None, max_iterations=30)
  - 首轮：match -> assemble -> run(session_mode=True, initial_messages=None)
  - 循环：打印返回文本 -> input() 读用户 -> 判 /exit / /done -> 否则 run(session_mode=True, initial_messages=session.messages) -> 续
  - 每轮 _save_state 落盘

### session.py（新建）
- SkillSession：数据持有（messages / snapshot / state_path / skill_name），不含执行逻辑

### cli.py
- 新增 session 命令（见第 7 节）

### models.py
- 暂不加 session: bool 字段，靠 CLI 命令触发；后续如需 skill 声明式启用再补

## 7. CLI 用法

```
# 进入单 skill 持续执行会话
skill-engine session "帮我重构模块X"

# 直接指定 skill（跳过 router 匹配）
skill-engine session "重构模块Y" --skill code-builder

# 指定工作目录与单轮子任务迭代上限
skill-engine session "分析这个仓库" -w ./src --max-iter 40

# 只指定 skill 与工作目录，不带初始请求（进入后先看用法提示，再输第一条指令）
skill-engine session -s code-builder -w /d/Code/PycharmProjects/demo-project
```

三种等价入口（免安装调试推荐第 2 种）：

```
skill-engine session ...                          # 安装后的 console_scripts
PYTHONPATH=src python -m skill_engine session ... # 走 __main__.py
PYTHONPATH=src python -m skill_engine.cli session ...
```

`cli.py` 的 `main()` 是统一入口，`__main__.py` 与 `[project.scripts]` 都指向它。

进入后：

```
[code-builder] 正在分析模块X...
[code-builder] 已完成，还有什么需要我做的吗？
> 现在帮我重构模块Y
[code-builder] 好的，分析模块Y...
[code-builder] 已完成
> /exit
会话已结束。
```

## 8. 兼容性影响（已核实）

- 普通 run 命令：零改动，run() 加参默认兼容。
- 现有 3 个 human_in_loop skill（code-builder / code-edit-file / skill-interview-builder）：不用 session 命令时行为 100% 不变；其 stop_when 均有值（code-builder="已完成|改动完成|已汇报|任务圆满完成|再见"、code-edit-file="已完成|已修改|已应用|任务圆满完成|再见"、skill-interview-builder="已完成"），但 session 模式按决策 2 禁用内部 should_stop 判定，故这些关键词在 session 内不触发终止——仅作为普通文本，子任务结束后控制权交回 REPL 等下条指令。
- 其他 10 个 CLI 命令：实现零改动。
- MCP / 审批 / 上下文压缩：全在 run() 内，session 复用自动继承。
- 文件快照需显式跨轮传递（见 8.1），否则每轮新建实例会把检查点覆盖成上一轮结果。
- 唯一决策点（已确认）：session 模式下禁用 run() 内部 should_stop / 内部 read，改由 ask_user 工具 + 外层 /exit 判定，避免双重提问。

### 8.1 会话级文件快照（实现补充）

`run()` 原本每次调用都 `self._snapshot = FileSnapshot(base_dir)`。FileSnapshot 的
manifest 会从磁盘加载，但 `_recorded` 集合是新的——于是第 2 轮对同一文件的首次写入
会重新 record，把第 1 轮记录的 `.bak` 覆盖成「第 1 轮结束时的内容」，`restore_file`
就只能回滚到本轮起点。

修复：`run()` 新增 `snapshot` 参数支持外部注入；`SkillSession` 持有一个 FileSnapshot，
`_repl_loop` 每轮传同一实例，`_recorded` 得以跨轮保留，检查点稳定在会话起点。
普通 run 不传该参数，行为不变（检查点=本次运行起点）。

### 8.2 空输入语义

直接回车不再回退成重跑原始 query（那会把第一轮请求重做一遍），改为追加一条
`Runner._CONTINUE_HINT`（"继续（沿用上文，接着往下做；不要重头开始）"）让 LLM
基于历史续写。仅当会话尚无历史时才使用原始 query。

### 8.3 状态文件的 session_mode 标记

`_save_state` 落盘时记录 `session_mode`。若该状态被普通 `run --resume-from` 载入，
run() 打 warning 提示：ask_user 工具不可用、不会在轮末交还控制权，应改用
`session --resume-from`。

### 8.4 无初始 query 启动（skill 自我介绍）

`session` 的位置参数改为可选。省略时必须配合 `-s/--skill`（否则无从路由，CLI 直接
报错并给出两种正确写法）。此时：

1. `run_repl` 不用空 query 起轮，改为先打印 `Runner._format_skill_hint(skill)`；
2. 提示内容全部来自 frontmatter（`description` / `when_to_use` / `argument_hint` /
   `arguments` / `allowed_tools` / `mcp_servers`），一律 `getattr` 兜底——缺字段只是
   少一行，不阻塞会话；因此**任意 skill 自动获得该提示**，无需逐个改 SKILL.md；
3. `_repl_loop` 首轮判定加 `query.strip()`，落到"等用户指令"分支；
4. 会话尚无历史时直接回车不再空跑一轮 LLM（原逻辑会以空 `$ARGUMENTS` 起轮），
   改为重新提示 `[session] 请输入一条指令`。

## 9. 安全边界

- 安全审批、文件快照、MCP 合并沿用 run() 现有逻辑，session 模式不降级。
- /exit 不丢状态：最后一轮已落盘，可后续 resume 续接。

## 10. 工作量估算

- 核心 ~280-410 行（run 加参 + ask_user/stop + run_repl + session.py + cli）
- 含测试 ~500 行
- 设计文档另计

## 11. 测试计划

tests/test_repl_session.py：
- 续轮：首轮 run 后 messages 进 session，次轮 run 能续上下文
- session 落盘往返：exit 后用 resume_from 续接
- ask_user 暂停：mock human_io 验证暂停/续跑
- /exit 退出：REPL 干净结束

## 12. 风险与缓解

- 双重提问：session_mode 禁用内部循环（已确认）。
- MCP 每轮重连开销：可在 SkillSession 持有连接复用，或接受每轮重连（方案 A 已是每调用重连）。
- 上下文膨胀：复用 ContextManager 压缩（run() 内已有）。

## 13. 决策点（已确认）

1. 语义信号：采用 ask_user + stop 工具（非 stop_when 关键词）—— 2026-07-31 确认
2. session 模式禁用 run() 内部 should_stop / 内部 read，由 ask_user + 外层 /exit 判定 —— 2026-07-31 用户确认
3. session 命令默认档位：档位 B（tool-dispatch）
4. models.py 暂不新增 session 字段，靠 CLI 命令触发
