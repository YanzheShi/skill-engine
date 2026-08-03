# code-builder 对标工业级 Coding Agent —— 差距分析与提升设计

> 状态：设计稿 v2（未动代码）。日期：2026-08-04
> v2 增量：§4 通用性兼容设计（对其他 skill 的影响逐项判定）、§5 差距总表增加代码量估算、附录 A 开源前安全清单
> 对标对象：Claude Code / Cline / pi-coding-agent（下称"工业三件套"）
> 前提：本文延续 `large-code-capability-design.md` 的分层纪律 —— **通用能力沉引擎核心，代码专用能力留 skill 层**。
> 附加验收标准（v2）：**每项新能力落地时必须回答"非 coding skill 会怎样"，答案只能是"无感知"或"可选启用"**（详见 §4）。

---

## 0. TL;DR

引擎的"框架面"已经相当完整：路由、审批、快照、session 持久化、MCP、工具可插拔、上下文压缩全都有，这在自研 agent 里属于少数派完成度。code-builder 的 SKILL.md 工作流（项目感知→澄清→计划确认→骨架优先→渐进降级→汇报）与 Cline 的最佳实践同构，prompt 层不输。

**但"编码能力"的差距不在 prompt，在引擎的编码基础设施。** 核心差距按影响排序：

| 级别 | 差距 | 一句话本质 |
|---|---|---|
| S0 | 编辑一致性无保障 | LLM 拿"记忆中的文件内容"做 edit，引擎不校验是否读过、文件是否已变 |
| S0 | 检索能力弱 | 纯 Python rglob 正则搜索，不认 .gitignore，无符号级导航，大仓库不可用 |
| S0 | 上下文工程不适配长会话 | 8192 预算 + 有损自由摘要 → session 越长越健忘，edit oldText 与真实文件漂移 |
| S0 | 验证闭环没打通 | bash 30s 超时且 LLM 不可调，测试跑不完；"改完必须验证"只靠 prompt 自觉 |
| S1 | 无任务结构 | 没有 todo 工具、没有 Plan 模式、没有子代理，单上下文一条道走到黑 |
| S1 | 编辑无确认、无边界 | 写盘无 diff 预览；working_root 不是权限边界，绝对路径可写穿项目外 |
| S1 | 交互与可观测性弱 | 无流式输出、无成本统计、无 diff 渲染，长时间等待黑盒 |
| S2 | 无评测体系 | 没有 coding 任务评测集，"追上 Cline"永远只是感觉 |

路线图（§6）：P0 修"犯低级错误"（约 2 周）→ P1 修"可信赖、看得见"（约 3-4 周）→ P2 修"规模化、可度量"（约 1-2 月）。全量代码量估算约 6,400 行（生产 ~4,400 + 测试 ~2,000），见 §5。

**通用性承诺**：本项目定位是通用 skill 执行引擎。上述所有改进按 §4 的判定分为三类——透明增强（其他 skill 无感知）、frontmatter 可选启用（默认关）、以及两个已修正默认值的例外项（FileStateTracker 软约束化、containment 改为可写根集合）。每项落地都要过"非 coding skill 回归冒烟"（§4.4）。

---

## 1. 现状盘点（我们已经有什么）

| 能力 | 现状 | 代码证据 |
|---|---|---|
| Agent 循环 | 标准 LLM→tool→observe 循环，支持 stop 工具、多 tool_calls 串行执行 | `tool_dispatch.py` ToolDispatchRunner |
| 内建工具 | bash / read_file / write_file / edit_file / search_files / stop / web_search / get_current_time | `tool_defs.py` |
| 编辑容错 | oldText 精确匹配 + 行级模糊兜底（strip/空白归一化窗口匹配） | `_apply_edits` / `_fuzzy_find` |
| 代码域工具 | cb_list_files（目录树）/ cb_ast_map（Python AST 签名）/ cb_git_checkpoint | `skills/code-builder/tools.py` |
| 上下文管理 | token 预算（默认 8192，chars//4）+ LLM 摘要压缩 + 轮次感知边界 | `context_manager.py` |
| 快照回滚 | 写前自动记录原内容（.skill_engine_snapshots/）+ restore_file 工具 | `snapshot.py` |
| Session | REPL 外层循环 + messages/snapshot/审批缓存跨轮 + 落盘续跑 + ask_user 轮内暂停 + Ctrl+C 不丢状态 | `runner.py` run_repl / SkillSession |
| 安全 | strict/permissive/off 三档 + 会话级审批缓存（y/Y/N/r/A）+ RISKY_FILENAMES + allowlist | `scanner.py` / `runner._check_approval` |
| 环境注入 | OS/shell/工作目录/路径风格显式告知模型；stderr 失败模式→纠正 hint | `build_env_header` / `_diagnose_shell_error` |
| 扩展 | extra_tools 可插拔 + MCP（stdio/HTTP/SSE） | `tool_defs.load_skill_tools` / `mcp_client.py` |
| 工作流 prompt | 四步工作流、POC 优先、骨架优先、渐进降级、一致性检查 | `skills/code-builder/SKILL.md` |

**强项要说清楚**：审批粒度 + 会话级缓存、快照回滚、环境头注入、shell 错误 hint、session 落盘续跑——这些细节说明我们踩过 agent 的真实坑，很多开源 coding agent 都没有。差距集中在"编码主链路"（感知→编辑→验证）和"任务结构"上。

---

## 2. 对标：工业三件套各自强在哪

对标前先校准参照系，避免拿别人的营销点当尺子：

- **Claude Code**：胜在 *上下文工程*（自动/手动 compact、结构化摘要、工具输出 micro-compaction、prompt caching）、*任务结构*（TodoWrite、Plan 模式、子代理 Task）、*权限模型*（allow/deny 规则到路径级、沙箱）、*项目记忆*（CLAUDE.md）、*一致性机制*（read-before-write 状态跟踪）。
- **Cline**：胜在 *人机协作的编辑确认*（每个 search/replace 出 diff，用户 approve/reject）、*Plan/Act 双模式*、*checkpoints*（shadow git 全量快照，可回滚到任意步骤）、*代码库感知*（list_code_definition_names，tree-sitter 多语言符号地图）。
- **pi-coding-agent**：极简路线的代表——胜在 *最小完备工具集* + *会话树（分支/回退到任意节点）* + *扩展系统*，以及把"steering 文件"（少量项目说明注入）做到极致。它证明：工具少没关系，**会话可回退、上下文可控**才是硬通货。

共性抽出来就是工业级 coding agent 的五根支柱：

```
① 编辑可靠性   —— read-before-write 状态机、diff 预览、checkpoint 回滚
② 代码库感知   —— gitignore-aware 快速搜索、符号导航、结构地图
③ 上下文工程   —— 结构化压缩、micro-compaction、缓存友好、预算贴着模型窗口
④ 任务结构     —— todo/plan/subagent，让长任务可分解、可暂停、可分工
⑤ 验证闭环     —— 测试/构建可跑（时长、流式）、失败信号结构化回灌驱动修复
```

下面按这五根支柱 + 四个支撑面（安全、记忆、交互、评测）逐一对比。

---

## 3. 逐维度差距分析

### 3.1 编辑可靠性（差距：大）

| 机制 | 我们 | Claude Code | Cline |
|---|---|---|---|
| 编辑前必须读过文件 | ❌ 无校验 | ✅ 跟踪文件读取状态，未读过的文件拒绝编辑 | ✅ 基于 read 后的内容出 diff |
| 文件外部变更检测 | ❌ 无 mtime/hash 校验 | ✅ stale 检测，要求重读 | ✅ checkpoint 对比 |
| 写前 diff 预览/确认 | ❌ 直接落盘 | 编辑走权限确认 | ✅ 每次编辑 diff + approve/reject |
| 全量覆盖保护 | ⚠️ write_file 可静默覆盖已有文件 | ✅ 覆盖已有文件需确认 | ✅ diff 可见 |
| 回滚粒度 | ✅ 单文件快照 + git checkpoint | checkpoint/undo | ✅ shadow git 到每一步 |

**问题展开**：我们已有模糊匹配兜底（好），但缺的是**一致性状态机**。典型翻车链路：session 第 8 轮，上下文压缩把 `foo.py` 原文摘要掉了 → LLM 凭摘要里的"印象"发 edit_file → oldText 对不上 → 报错 → LLM 再猜一次 → 浪费 2-3 轮迭代，运气差就把错的内容模糊匹配进去。Claude Code 用"文件读状态 + 陈旧检测"从根上掐断这条链。

**设计草案（S0-1，FileStateTracker，沉引擎核心）**：

```python
class FileStateTracker:
    """跟踪 session 内每个文件的'已知版本'，编辑前校验一致性。

    不改变任何工具签名，作为 edit_file/write_file 的前置门。
    通用性设计（见 §4.3）：默认软约束，skill 可升级为硬约束。
    """
    # path -> {"hash": sha1(内容), "mtime": float, "read_turn": int}
    known: dict[str, dict]
    strict: bool          # False=软约束（默认）；True=硬约束（frontmatter strict_file_tracking）

    def on_read(self, path, content): ...       # read_file 成功后登记
    def on_write(self, path, content): ...      # 自己写盘后更新登记
    def invalidate_all(self): ...               # bash 执行后保守失效（见下）
    def check_editable(self, path) -> tuple[bool, str]:
        """未读过 / 磁盘 hash 变了 -> 产出'先 read_file'提示。
           软约束：返回 (True, warning)，提示随 tool result 回灌，不阻断；
           硬约束：返回 (False, error)，拒绝本次 edit。"""
```

要点：
- session 模式跨轮持有（放 SkillSession，与 snapshot 同级）；
- 校验失败信息直接作为 tool result 回灌 LLM，它会自己补 read——把 SKILL.md 里"edit_file 前先 read"的软约定变成引擎机制；
- **软约束为默认值**（通用性要求，见 §4.3）：对"不读直接改"的小 skill 只提示不阻断；code-builder 声明 `strict_file_tracking: true` 升级为硬约束；
- **bash 执行后 `invalidate_all()`**：bash 可能改了任何文件而 tracker 不知情，不失效会导致下一轮 edit 被误判 stale。保守全失效的代价只是多一次 read，可接受；
- 配合压缩：摘要里写"foo.py 已修改、最新版本已登记"，LLM 想 edit 会被引导重读，"摘要丢细节"从致命伤降级为小代价。

**设计草案（S1-3，diff 预览门）**：edit_file/write_file 落盘前生成 unified diff（difflib 即可，零依赖），经 human_io 展示，frontmatter `confirm_edits: true` 时逐次确认、`confirm_edits: batch` 时按文件批确认。**默认关闭**，session 模式下的 code-builder 声明 batch。这是 Cline 用户信任感的最大来源，成本低，优先级可以比 P1 更靠前。

### 3.2 代码库感知与检索（差距：大）

现状问题（按严重度）：
1. `search_files` 纯 Python rglob 逐行正则：不认 `.gitignore`（只跳点开头的目录），50 条上限，大仓库秒级~分钟级，且二进制/依赖目录靠运气跳过；
2. `cb_ast_map` 只支持 Python；TS/Go/Rust/Java 项目直接没有结构地图；
3. **没有符号级导航**：找"谁调用了这个函数"只能 grep 文本，大型重构的一致性检查（SKILL.md 第四步要求）又慢又漏。

工业三件套：Claude Code 的 Grep/Glob 是 ripgrep 底座 + gitignore 原生支持；Cline 有 tree-sitter 多语言符号提取；pi 同样是 rg + tree-sitter。

**设计（S0-2）**：
- `search_files` 双实现：**ripgrep 优先**（`rg --json`，subprocess 调用，尊重 .gitignore，毫秒级），无 rg 二进制时回退现有纯 Python 实现。输出格式不变，LLM 无感，其他 skill 亦无感（见 §4.1）；
- 结果上限从 50 提到参数化（默认 100，附 `total_count` 让 LLM 知道被截断）；
- 符号导航分两步走（守住分层纪律）：
  - 第一步（skill 层）：`cb_ast_map` 扩展 goto——`cb_find_symbol(name)` 返回定义位置 + 全库引用位置（Python 用 ast，引用用 rg 文本匹配近似，够用）；
  - 第二步（视多语言需求）：tree-sitter 多语言签名地图，或对接 LSP（pyright/gopls）。这属于 P2，先不做。

### 3.3 上下文工程（差距：大，session 功能让它从隐性变显性）

session 上线后这是**最致命**的一块：多轮持续修改代码 = 长会话，而当前 ContextManager 的三个参数都是为短 run 调的：

1. **预算 8192 太小**。coding agent 的工作集（任务 + 计划 + 若干文件内容 + 测试输出）动辄 30k-80k token。8192 意味着平均每 2-3 轮就压缩一次，历史信息反复有损蒸馏。应按模型窗口动态取（如 window × 0.5），frontmatter `context_budget` 升级为必填/自检；**只扩不缩**：已声明 `context_budget` 的 skill 继续用自己的值，未声明的才用新默认；
2. **估算 chars//4 对中文严重失真**。中文 1 字符 ≈ 1-2 token，chars//4 会低估 4-8 倍 → 真实窗口被打爆时引擎还以为很宽裕。至少要按"ASCII/非 ASCII 分别计权"，理想接模型方的 token 计数；
3. **自由摘要是有损且不可验证的**。压缩 prompt 只说"保留文件路径与关键内容"，但压完 LLM 若还想 edit 那些文件，原文已经没了（见 3.1 的翻车链路）；
4. **无 micro-compaction**。工业做法是先"降级旧的大块工具输出"（某次 8000 字的 pytest 输出、某次 5000 字的 read，几轮之后只剩诊断价值），最后才动对话结构。我们一上来就摘要整个历史段；
5. **无缓存友好设计**。OpenAI 兼容侧的 prefix cache / Anthropic 的 prompt caching 都要求前缀稳定。我们每轮 messages 结构基本稳定（好），但环境头 + skill prompt 若随 assemble 变动会击穿缓存。长会话下缓存 = 成本 ×0.1，值得专门设计；
6. 压缩用的 llm 与主模型同一个（贵且慢）。`LLM_MODEL_ALT` 配置位已经留好了，没人用。

**设计（S0-3，三级降级压缩策略）**：

```
L1 micro-compaction（每轮检查，无损于任务语义）
   超过 N 轮之前的 tool 消息中 >1500 字的内容，替换为
   "[已折叠: bash 输出 7.8k 字, 摘要: 3 个测试失败于 xxx]"
   ——规则化即可，不需要 LLM
   ⚠️ 通用性注意（§4.3）：read_file 结果被折叠后，LLM 再 edit 该文件
   会被 FileStateTracker 引导重读，两机制联动闭环；
   skill 可声明 compact_tool_output: false 整体关闭 L1

L2 结构化压缩（接近预算时，替代当前自由摘要）
   让 LLM 按固定 schema 产出 <task_state>，**默认模板任务中立**：
   - 原始请求（逐字保留首条 user 指令）
   - 已完成的动作：[{对象, 做了什么}]
   - 当前进行到：...
   - 待办：[...]
   - 关键对象清单（只留引用，不留原文——要细节就去工具重取）
   coding 版（已改文件/验证状态）作为 code-builder 的 frontmatter
   compress_template 覆盖。schema 化让压缩结果可测试、可回归

L3 截断兜底（预算被打穿时）
   保留首条 prompt + 最近 keep_recent 轮，其余直接丢弃并显式告知 LLM
   "历史已被截断，涉及旧内容请先用工具重新获取核实"
```

配合 FileStateTracker：压缩后 LLM 对任何文件 edit 都会被引导/强制重读，"摘要丢细节"从致命伤降级为小代价。

### 3.4 执行与验证闭环（差距：中到大）

1. **超时**：CLI 给 Executor 30s，LLM 无法按命令调整。`pytest tests/`、`pip install -e .`、前端 build 很容易超 30s → 被杀 → LLM 看到 `[超时: 30s]` → 换个姿势再试 → 空转。设计：bash 工具加 `timeout` 参数（引擎设硬上限如 600s，frontmatter 可调）。纯新增可选参数，不传即现状，对所有 skill 零影响；
2. **长任务**：P2 再考虑后台执行 + poll（bash `background: true` 返回 job id + 后续 `poll_job`），对齐 Claude Code 的后台任务；
3. **验证不是闭环**：SKILL.md 说"改完必须验证"，但引擎不保证。设计两个引擎级挂钩点：
   - frontmatter 可选 `verify_command`（如 `pytest -x -q`）：每轮 edit/write 结束后引擎自动跑，失败输出直接作为 tool 观察回灌，形成"改→验→修"自动循环，不依赖 LLM 自觉。**未声明的 skill 完全不触发**；
   - 观察侧：pytest 失败输出做结构化提取（`FAILED tests/x.py::test_y - AssertionError: ...` 提取成清单），减少 token 又提升定位精度；
4. **并行工具调用**：parse 已支持一条消息多个 tool_calls，但执行是串行 for。read/search 类只读工具可并行（实现简单：asyncio.gather 或线程池，只对 `read_only` 白名单工具生效，见 §7 handler 重构）。大任务里"同时读 5 个文件"很常见，串行每个 0.5s LLM 间隔 + 工具耗时，累积可观。有副作用的工具一律串行，保证其他 skill 行为不变。

### 3.5 任务结构：todo / Plan 模式 / 子代理（差距：大）

当前整个任务是"一根线"：LLM 在单上下文里探索→计划→实现→验证，计划只存在于某条 assistant 文本里，引擎看不见、用户没法追踪、压缩时还可能被摘掉。

| 机制 | 我们 | 工业做法 |
|---|---|---|
| Todo | ❌ | Claude Code TodoWrite：引擎持有任务清单，逐项打勾，UI 可见，压缩时强制保留 |
| Plan 模式 | ❌（靠 SKILL.md"等用户确认"的文本约定） | Plan 模式 = 只读工具集探索 → 产出计划 → 用户批准 → 才解锁写工具 |
| 子代理 | ❌ | Claude Code Task / Cline 子任务：探索、实现分上下文，主上下文只收摘要 |

**设计（S1-1 todo 工具，沉引擎核心，opt-in 绑定）**：

```python
@tool
def todo(items: list[dict] | None = None,
         update: dict | None = None) -> str:
    """items=[{id, title, status: pending|doing|done}] 全量设置；
    update={id, status} 单项更新。返回当前清单。"""
```

- **绑定 opt-in**（通用性要求，§4.2）：frontmatter `use_todo: true` 才把该工具绑进该 skill 的工具集。工具描述本身占 token 且可能诱导小 skill 的 LLM 去"建清单"，默认不绑；
- 清单随 session 落盘（进 state json），压缩时与 `<task_state>` 合并，**永不被摘要掉**；
- REPL 每轮边界打印清单进度（用户可见性，无清单时不打印）；
- SKILL.md 的"第二步输出计划"改为"计划必须写入 todo"——计划从聊天记录升级为引擎一等公民。

**设计（S1-1b Plan 门闩，opt-in）**：不必做成独立模式，做成**工具集门闩**：frontmatter `plan_mode: true` 时，run() 第一阶段只绑定只读工具（read_file/search_files/cb_list_files/cb_ast_map/bash 限只读 allowlist），LLM 输出计划文本交还用户；用户回复确认后 REPL 以"解锁写工具"重新起轮。session 的轮边界机制（session_turn_end）天然适配，几乎不用新机制。依赖 §7 handler 的 `read_only` 标志。

**设计（S1-2 子代理，P2，opt-in 工具）**：`dispatch_task(goal, tools="read_only")` 内建工具：内部新建一个 ToolDispatchRunner + 独立 ContextManager，跑完只把**摘要**（≤1000 字）回灌主上下文。用途：大仓库探索、跨文件影响面排查、POC 验证。复用现有全部设施（审批/快照/executor），实现成本中等，但对 token 经济是质变。注意：子代理禁写（只读工具集），写操作永远在主上下文发生，避免多写者冲突。

### 3.6 安全与权限（差距：中，部分是我们的强项）

强项：三档模式、会话审批缓存、allowlist、危险文件名。差距：

1. **working_root 不是权限边界**：`_resolve_path` 对绝对路径透传，edit_file/write_file 可以写项目外任意位置（RISKY_FILENAMES 只挡了少数敏感文件名）。Claude Code 的 permissions 是到路径 pattern 的。
   **设计（S1-4，修正版：可写根集合，opt-in）**——原方案"默认限制在 working_root 内"被否决，因为会弄坏本职就往工作目录外写的 skill（如 `write-output` 写产出目录、`backup-config`、依赖 `$VAULT_PATH` 的 skill）。修正为：

   ```yaml
   # frontmatter：只有声明了 writable_roots 的 skill 才启用 containment
   writable_roots: ["$WORKING_ROOT"]                  # code-builder：只许写项目内
   writable_roots: ["$WORKING_ROOT", "$VAULT_PATH"]   # vault 类 skill：多个可写根
   ```

   引擎对文件写操作解析后校验是否落在任一声明的可写根子树内，越界则拒绝并回灌错误；**未声明 = 不限制（维持现状）**，存量 skill 零影响。
2. **无审计日志**：step_results 落了盘但散在 state json。设计：session 级 append-only transcript（每轮一条 JSONL：工具、参数、结果摘要、审批决定），既是审计也是评测数据源（见 3.9）；
3. 沙箱（容器/chroot）：工业界也在探索期，我们单机 CLI 定位可后置，P2+ 再议。

### 3.7 持久化与记忆（差距：中）

- **已有**：session 落盘、resume、快照跨轮。这块不落后；
- **缺项目记忆**：Claude Code 的 CLAUDE.md / pi 的 steering 文件——把"本项目构建命令、代码约定、已知坑"固化成文件注入 system prompt。我们的 env header 只注入 OS/shell。设计（S2-2）：working_root 下 `.skill-engine/PROJECT.md`（或兼容直接读 CLAUDE.md）存在则拼入环境头。文件不存在则跳过，所有 skill 零影响。成本≈半天，对反复 session 同一项目的体验提升极大；
- **缺会话回退**：pi 的会话树是它的招牌——回退到任意历史节点（messages + 文件状态同时回滚）。我们有 messages 落盘 + 文件快照，只差把两者用"轮号"串起来：每轮 boundary 记一个 checkpoint_id（messages 截断点 + 当时的 snapshot manifest 副本）。设计（S2-3），用户在 REPL 里 `:rewind 3` 回到第 3 轮结束时的对话与文件状态。

### 3.8 交互与可观测性（差距：中）

1. **无流式**：`llm_with_tools.invoke()` 整轮等完才输出。长思考时用户面对死寂终端。Claude Code/Cline 都是 token 级流式。设计（S1-5）：改 `stream()`，content chunk 实时经 human_io 打印，tool_calls 在流结束后解析。HumanIO 抽象已有，加 `emit_chunk` 即可；Gradio UI 需适配（增量渲染），CLI 直接受益；
2. **无成本统计**：token 用量/费用完全不感知。设计：每轮记 prompt/completion tokens（LangChain response 里有），轮末打印累计，进 transcript。这对调 context_budget 也是数据支撑；
3. **无 diff 渲染**：见 3.1；
4. Web UI（Gradio）与 session 的结合度未验证，P2 议题。

### 3.9 工程评测（差距：从 0 到 1，但决定一切）

三件套迭代快的真正原因是**拿真实任务回归**。我们现在改引擎全靠单测（工具级）+ 手感。没有任务级指标，就无法回答"这次改动让编码能力变强还是变弱"，也无法证明"没有弄坏其他 skill"（后者见 §4.4 的冒烟机制）。

**设计（S2-1，评测 harness，建议提前到 P1 末尾启动）**：
- 任务集分两组：
  - **编码能力组**：10-20 个可自动判定的 coding 任务，覆盖：单文件修 bug / 加函数+测试 / 跨文件重命名 / 加依赖并更新声明 / 读懂大模块后定点修改。每个任务 = 一个 fixture 仓库 + 任务描述 + 判定脚本（pytest 通过 / 断言 diff）；
  - **通用性回归组**（v2 新增）：见 §4.4；
- 指标：任务成功率、平均迭代数、平均 token、edit 失败率、验证命令通过率；
- 每次引擎 PR 跑一遍，输出对比表。transcript（3.6）直接作为数据源。

---

## 4. 通用性兼容设计（v2：会弄坏其他 skill 吗？）

本项目定位是通用 skill 执行引擎，coding 只是其中一个 skill。每项改进按对其他 skill 的影响分三类判定，**这是每个功能落地时的验收门**：

### 4.1 ① 透明增强——接口与语义不变，其他 skill 无感知

| 项 | 无影响的原因 |
|---|---|
| search_files 换 ripgrep（S0-2） | 接口/输出格式不变，只是更快、认 gitignore；无 rg 时回退现状 |
| bash 加 timeout 参数（S0-4） | 纯新增可选参数，不传即现行为 |
| 上下文预算贴模型窗口（S0-3） | 只扩不缩；已声明 context_budget 的 skill 不变 |
| 流式输出 / 成本统计 / transcript（S1-5） | 引擎管道层，skill 侧零改动 |
| handler 注册表重构（S1-6） | 行为等价重写，靠测试保证 |
| PROJECT.md（S2-2）/ :rewind（S2-3）/ 模型路由（S2-4）/ 符号导航（S2-6） | "有则生效、无则跳过"或 skill 层工具 |

### 4.2 ② Frontmatter 可选启用——默认关闭，skill 声明才生效

| 项 | 开关字段 | 启用者 |
|---|---|---|
| 自动验证钩子（S0-4） | `verify_command` | code-builder |
| diff 预览确认（S1-3） | `confirm_edits: off/true/batch` | code-builder（batch） |
| Plan 门闩（S1-1b） | `plan_mode: true` | code-builder 可选 |
| todo 工具绑定（S1-1） | `use_todo: true` | code-builder |
| 子代理（S1-2） | `subagents: true` | code-builder |
| 硬约束文件跟踪（S0-1） | `strict_file_tracking: true` | code-builder |
| 可写根 containment（S1-4） | `writable_roots: [...]` | code-builder / vault 类按需 |
| coding 版压缩模板（S0-3） | `compress_template: coding` | code-builder |

未声明的 skill（write-output、interview-simulator-master、english-poetry-writer、mind-map 等）行为与现在**逐字节一致**。

### 4.3 ③ 三个必须修正默认值的例外（设计已按此修正）

1. **FileStateTracker 不能默认硬约束**。理由有二：a) 存在"不读直接改"的合法小 skill（拿模板改配置），硬门会让它们报错；b) **bash 改了文件而 tracker 不知情**，下一轮 edit 会被误判 stale。修正：默认软约束（提示回灌不阻断）+ bash 后保守 `invalidate_all()` + code-builder 声明升级为硬约束（§3.1）。
2. **containment 不能默认开**。`write-output` / `backup-config` / vault 类 skill 的本职就是写 working_root 之外（仓库配置里的 `$VAULT_PATH` 即实证）。默认限制会直接残废它们。修正：可写根集合 opt-in（§3.6）。
3. **压缩 schema 默认值必须任务中立**。原草案的 `<task_state>` 字段（已改文件/验证状态）偏 coding，沉到核心会把 coding 语义泄漏给写诗、面试类 skill。修正：默认模板通用化（原始请求/已完成动作/未决事项/关键对象引用），coding 版作为 code-builder 的模板覆盖（§3.3）。现有的自由摘要 prompt 本身已偏 coding，本次一并中立化。

### 4.4 验证机制：冒烟回归 skill

设计保证之外还需要运行证明。从现有 20 个 skill 挑 2-3 个非 coding 的做**冒烟回归 skill**（建议 `write-output`、`interview-simulator-master`、`leetcode-solution-writer`）：每次引擎改动后跑一遍各自的最小任务，断言正常走完。并入 §3.9 评测 harness 作为"通用性回归组"，让"不破坏其他 skill"从口头承诺变成每次 PR 的绿灯。

---

## 5. 差距总表（含代码量估算）

代码量按本项目现有风格（docstring/注释较密、测试配套）估算，含测试；"搬移"指重构中位置变化的既有代码，不计入新增。

| # | 差距 | 级别 | 涉及模块 | 人天 | 代码量（生产+测试） |
|---|---|---|---|---|---|
| S0-1 | FileStateTracker：软/硬约束 + 陈旧检测 + bash 失效 | S0 | tool_dispatch + runner(SkillSession) | 3 | ~350 |
| S0-2 | search_files 接 ripgrep + gitignore + 上限参数化 | S0 | tool_dispatch（搜索分支） | 2 | ~300 |
| S0-3 | 上下文三级降级 + 预算贴窗口 + 中文估算修正 + 中立 schema | S0 | context_manager + models | 5 | ~450 |
| S0-4 | bash timeout 参数 + verify_command 钩子 + 测试失败结构化 | S0 | tool_defs + tool_dispatch + executor | 3 | ~350 |
| S1-1 | todo 工具（opt-in 绑定）+ 轮边界展示 + 压缩保留 | S1 | tool_defs + tool_dispatch + context_manager | 4 | ~380 |
| S1-1b | Plan 门闩（只读阶段→批准→解锁写） | S1 | tool_dispatch + runner(session) | 3 | ~380 |
| S1-2 | 子代理 dispatch_task（只读） | S1/P2 | tool_dispatch | 6 | ~600 |
| S1-3 | edit/write diff 预览 + 可选确认 | S1 | tool_dispatch + human_io | 3 | ~250 |
| S1-4 | 可写根集合 containment（opt-in） | S1 | tool_dispatch(_resolve_path 处) | 2 | ~140 |
| S1-5 | 流式输出 + token/成本统计 + JSONL transcript | S1 | tool_dispatch + human_io + runner | 5 | ~450 |
| S1-6 | tool_dispatch 重构：工具执行注册表化（见 §7 前置债） | S1 | tool_dispatch | 4 | ~400 新增（另有 ~700 行搬移） |
| S2-1 | 评测 harness（编码组 + 通用性回归组） | S2 | tests/ + fixture 仓 | 6 | ~1,000（大头是 fixture） |
| S2-2 | PROJECT.md 项目记忆注入 | S2 | tool_dispatch(build_env_header) | 0.5 | ~40 |
| S2-3 | 会话 :rewind（messages+snapshot 按轮回退） | S2 | runner + snapshot | 5 | ~480 |
| S2-4 | 模型路由：压缩/路由用 LLM_MODEL_ALT | S2 | context_manager + router | 1 | ~60 |
| S2-5 | 只读工具并行执行 | S2 | tool_dispatch | 2 | ~120 |
| S2-6 | 多语言符号地图（tree-sitter/LSP） | S2 | skill 层 tools.py | 8 | ~300 |

分阶段合计：**P0 ≈ 1,700 行**（生产 ~1,100 + 测试 ~600）、**P1 ≈ 2,100 行**、**P2 ≈ 2,600 行**，全量 **≈ 6,400 行**。参照基数：现有 `src/skill_engine/` 约 6,000 行。

最小验证切片：S0-1 软约束版 + S0-4 的 bash timeout 参数，合计不到 400 行，session 连续改代码的翻车率即有肉眼可见的下降。

---

## 6. 分阶段路线图

### P0 —— 不再犯低级错误，长会话不崩（约 2 周，~1,700 行）
**S0 四项 + S1-3（diff 预览，因成本极低提前）**。主题是把"感知→编辑→验证"主链路做到工业及格线：
1. FileStateTracker（S0-1，软约束默认 + code-builder 声明硬约束）：掐断"凭记忆编辑"翻车链；
2. ripgrep 检索（S0-2）：大仓库可用；
3. 上下文三级降级（S0-3，中立 schema 默认）：session 长谈不健忘；
4. bash timeout + verify_command（S0-4）：验证真正跑得完、跑得自动；
5. diff 预览（S1-3，opt-in）：落盘前看得见改了什么。

验收：a) 在一个 ≥2 万行真实仓库上，连续 session 完成"跨 5 文件的特性开发 + 测试"，全程无一次因编辑漂移/上下文爆炸/超时导致的返工；b) §4.4 冒烟回归 skill 全部绿灯。

### P1 —— 可信赖、看得见（约 3-4 周，~2,100 行）
S1-6 重构（前置，见 §7）→ todo（S1-1）→ Plan 门闩（S1-1b）→ 可写根 containment（S1-4）→ 流式+成本+transcript（S1-5）→ 评测 harness 雏形（S2-1 提前启动，含通用性回归组）。主题：任务有结构、权限有边界、过程可观测、改动可回归。

### P2 —— 规模化、可度量（1-2 个月，~2,600 行）
子代理（S1-2）、会话 :rewind（S2-3）、PROJECT.md（S2-2）、模型路由（S2-4）、并行工具（S2-5）、多语言符号（S2-6）。主题：token 经济与多语言扩展，用评测集驱动每一项的取舍。

---

## 7. 前置工程债：tool_dispatch.py 必须先重构

**这是所有 P0/P1 项的共同前置。** `tool_dispatch.py` 已 1227 行，九个工具的实现以内联 if/elif 巨链写在 run() 循环里。S0 的每一项（read 状态门、搜索替换、压缩钩子、验证钩子）都要往这条链里插代码，再往后每加一个能力都在推高回归风险——项目 49 个测试文件的回归面全压在这一个文件上。

目标结构（设计草案，S1-6）：

```python
# 工具执行注册表：内建工具与 skill 工具走同一条路
class ToolHandler(Protocol):
    name: str
    read_only: bool                     # 供 Plan 门闩/并行白名单/子代理工具集使用
    def execute(self, args: dict, ctx: ToolContext) -> ToolResult: ...

class ToolContext:  # 替代现在散在循环里的局部状态
    base_dir: Path
    skill: Skill
    snapshot: FileSnapshot
    file_tracker: FileStateTracker
    approval: Callable
    messages: list                      # 观察回灌

TOOL_HANDLERS: dict[str, ToolHandler]   # bash/read_file/.../restore_file 各自一个类或函数
# run() 循环只剩：LLM 调用 → parse → 查注册表执行 → 回灌。
# skill extra_tools / MCP 工具注册为 GenericInvokeHandler（即现在的 else 分支）。
```

收益：S0/S1 各项变成"新增/装饰一个 handler"；read_only 标志直接支撑 Plan 门闩与并行白名单；handler 可单独单测。**注意**：这是行为等价重写，验收标准是既有 49 个测试 + §4.4 冒烟 skill 全绿。建议 P1 第一周做，P0 各项实现时已有意识地把新逻辑写成独立函数（不继续往巨链里堆），为重构铺路。

---

## 8. 风险与取舍

1. **ripgrep 分发**：bundle 二进制增加包体与跨平台维护；折中方案是"有 rg 用 rg、无则回退"，并在 README 建议安装。不阻塞。
2. **FileStateTracker 误伤与失效成本**：外部工具（用户手动、git checkout）改文件会被判 stale 引导重读——是正确行为，但要求 read_file 足够快（与 S0-2 联动）；bash 改文件导致的误判由保守 `invalidate_all()` 消化，代价是偶尔多一次 read。
3. **压缩 schema 化的模型依赖**：小模型产出 `<task_state>` 质量不稳。缓解：L2 压缩失败/格式错时回退自由摘要，再不行 L3 截断，三级兜底天然容错；压缩模型可用更强的主模型、日常对话用便宜模型（与 S2-4 一并设计）。
4. **verify_command 副作用**：自动验证可能在未完成的中间态反复跑测试，浪费 token。缓解：只在"一轮内所有 edit/write 完成后"跑一次，且结果只在失败时回灌。
5. **通用性回归风险（v2 新增）**：三类缓解叠加——§4.1/4.2 的分类判定（设计时）、§4.3 的三个默认值修正（评审时）、§4.4 冒烟回归 skill（运行时）。任何新功能 PR 描述必须回答"非 coding skill 会怎样"。
6. **todo 默认绑定改变全体 skill 的 LLM 行为**：工具描述占 token 且可能诱导无关 skill 建清单，故采用 opt-in 绑定而非默认绑定。
7. **保持通用引擎定位**：FileStateTracker / todo / containment / 压缩全部是领域无关能力，沉核心不违规；符号导航、git checkpoint 继续留 skill 层。每新增能力先过这条纪律 + §4 的判定。
8. **对标准确性声明**：Claude Code / Cline 的能力描述基于其公开文档与行为；pi-coding-agent 基于其开源仓库的公开资料，细节随版本演进，实施某一项前建议再核对一次最新行为。

---

## 9. 结语

我们现在的位置：**框架完成度 80%，编码主链路完成度 40%**。路由、审批、session、MCP 这些"平台件"已经不输甚至超前，真正拉开与 Claude Code / Cline 差距的是五根支柱里的前三根——编辑一致性、代码库感知、上下文工程，以及贯穿它们的验证闭环。P0 四项（约 1,700 行）做完，code-builder 就能从"能跑 demo"进入"敢接真实仓库的活"；再用评测 harness（含通用性回归组）把每一步提升变成可度量的数字，追赶就从感觉变成工程——同时守住"通用 skill 执行引擎"的定位不动摇。

---

## 10. 实施进度（P0）

| 日期 | Commit | 落地内容 | 新增用例 |
|---|---|---|---|
| 2026-08-04 | c3a5c8e | S0-1 FileStateTracker（软/硬约束 + bash 后失效 + session 跨轮）+ S0-4a bash timeout 参数（硬上限 600s） | +16 |
| 2026-08-04 | ce9dca3 | S0-2 search_files 双实现（ripgrep 优先含 --no-require-git、纯 Python 回退、max_results 参数化）+ S0-3 三级压缩（L1 折叠/L2 中立 schema/L3 截断、中文估算修正、预算默认 32768 可配）+ S0-4b verify_command 自动验证钩子（失败结构化回灌） | +29 |
| 2026-08-04 | 328c301 | S1-3 编辑 diff 预览：difflib unified diff + confirm_edits 确认门（'true' 逐次确认 / 'batch' 逐文件确认，首次批准后该文件会话内自动放行；非交互降级仅展示）；code-builder 已声明 batch | +11 |

**P0 状态：全部完成（6/6）** —— 编辑一致性、检索、上下文工程、验证闭环、diff 预览均已落地。
下一步为 P1：todo 工具 / Plan 门闩 / 可写根 containment / 流式与成本统计 / tool_dispatch handler 化重构（见 §6-§7）。

**回归纪律（已执行）**：每次改动在相同条件下分别运行当前树与 HEAD 基线（git archive 导出），
失败清单逐条 diff——存量失败（过期测试引用已删 API、VM 与 Windows 环境差异）单列，不计回归。
两轮 P0 切片均为零新增回归。

---

## 附录 A：开源前安全清单（2026-08-04 检查，08-05 复核修正）

仓库计划由 private 转 public。初查发现 `.env`（含真实 key：QWEN / DASHSCOPE / AGNES / SENSENOVA 的 `sk-*`、Judge0 密码、ADMIN_PASSWORD、SECRET_KEY_BASE、本地 VAULT_PATH）曾进入 git 对象库（Phase 1 提交 `c8daf3d`，后于 `2488410` 删除文件）。

**复核结论（可达性分析）：不构成泄露风险，无需 filter-repo。**

```bash
git branch -a --contains c8daf3d   # → 空：无任何本地/远程分支包含
git tag --contains c8daf3d         # → 空：无 tag 包含
git name-rev c8daf3d               # → stash~22：仅可从 refs/stash 到达
```

即：含 `.env` 的历史线只被一个 stash（"On feature/skill-creator: temp"，该分支已不存在）锚定在本地对象库，**从未存在于任何已推送分支**。`git push` 只传分支可达对象，`refs/stash` 从不被推送；转 public 后无人能取到该 commit。

**修正后的操作清单**：

1. 转 public 前在自己机器上刷新远程状态并复验一次：
   `git fetch --all --prune && git branch -r --contains c8daf3d`（应为空）；
2. 无需 filter-repo、无需 force push、无需轮换 key（key 从未离开本机；
   如求绝对稳妥，轮换成本低可自行选择）；
3. stash 处理二选一：**保留**（它永远不会被推送）；或先
   `git stash show -p stash@{0}` 确认无价值后 `git stash drop` +
   `git gc --prune=now` 彻底清除本地痕迹；
4. 转 public 后开启 GitHub secret scanning / push protection（公开仓库免费）；
   可选加 gitleaks pre-commit hook。

**方法教训**：扫描要用 `git log --all`（stash/tag 都算暴露面），但定性要看**可达性**
（`branch/tag --contains`）——"在对象库里"不等于"会被推送"。
