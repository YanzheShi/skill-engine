# skill-engine 编码智能体评测分析报告

| 项目 | 内容 |
|---|---|
| **报告名称** | skill-engine 编码智能体评测分析报告 |
| **版本** | V1.0 |
| **编制日期** | 2026-08-24 |
| **评估对象** | skill-engine（Agent 化编码执行引擎） |
| **评估范围** | 三种执行模式（tool_dispatch / session / MOA）能力、成本与兼容性；与 OpenCode 横向对比 |
| **数据来源** | code-agent-eval 评测框架（results/index.json 37 条 + moa_probe 真机配对实验）、skill-engine 源码契约复核 |
| **密级** | 内部 / Internal |
| **适用读者** | 技术负责人、研发效能团队、Agent 平台架构师 |

---

## 1. 执行摘要（Executive Summary）

本报告基于 **agent-agnostic 黑盒评测框架 code-agent-eval** 对 skill-engine 编码智能体进行系统化评估，覆盖其三种执行模式（tool_dispatch 基线 / session 多轮 / MOA 多模型协作），并完成与 OpenCode 的横向对比，以及对 skill-engine 代码更新后的**评测框架兼容性复核**。

**核心结论：**

1. **兼容性：通过。** code-agent-eval 在 skill-engine 代码更新后**无需改动即保持兼容**。评测驱动是纯黑盒子进程 + stdout 解析，三个契约面（CLI 命令/flag 拼写、成本行格式、工具调用 trace 行 `🔧`）均未破坏，默认 MOA plan 文件仍存在。
2. **基线能力：扎实。** skill-engine 基线在 7 档任务（t1–t7）上综合加权分（weighted_avg）介于 **0.45–0.89**，在正确性、工具使用、健壮性维度表现稳定；歧义类任务（t6）与重构类任务（t2）是主要失分点。
3. **MOA 不是免费增益，而是「条件增益」。** 同实例配对实验中：
   - **t6（歧义需求）：MOA 翻盘** —— 基线 oracle 通过率 0.0，MOA 达到 1.0，综合分 0.616 > 0.555，且 token 反而少 24%。
   - **t1（简单 bugfix）/ t2（重构）：MOA 劣化** —— 综合分下降 0.26–0.28，token 暴涨 116%–172%。
   - **t7（综合大型改动）：MOA 超时失败** —— 1200s 预算打满未完成，基线 0.828 完胜。
4. **选型建议：** MOA 只应在**需求歧义 / 需多视角校验**的任务上启用；简单、重构、超综合类任务应坚持基线模式，否则既降分又爆成本。
5. **与 OpenCode 对比：** 在 t1/t6 上，OpenCode-ACP 变体（0.95/0.91）优于 skill-engine 基线（0.89/0.64）与 MOA（0.67/0.62），显示单模型 + 强工具链的成熟方案仍有竞争力；skill-engine 的差异化价值在于**工程化成本埋点 + MOA 多模型协作可选**。

---

## 2. 评估背景与目标

### 2.1 背景
skill-engine 是一个 Agent 化编码执行引擎，提供三种执行模式：
- **tool_dispatch（档位 B，基线对比路径）**：单模型 + 原生 tool_dispatch 循环，对应普通 session 能力。
- **session（多轮会话 / REPL）**：支持多轮人机交互与粘贴多行输入。
- **MOA（Mixture of Agents，多模型协作）**：多个模型 worker 互审 + commander 决策，旨在提升复杂任务表现。

code-agent-eval 是一套 **agent-agnostic** 的编码智能体评测框架，通过黑盒子进程驱动待测 Agent、解析其 stdout 中的成本与工具调用信息，并用统一的 8 维评分体系对账。

### 2.2 评估目标
1. 验证 skill-engine 代码更新后，code-agent-eval 评测框架是否仍兼容（不影响持续评测能力）。
2. 量化 skill-engine 基线在 t1–t7 各档任务上的能力画像（含 8 维明细）。
3. 通过同实例配对实验，明确 MOA 相较基线的**增益/亏损边界**与成本代价。
4. 与 OpenCode 同类能力做横向对比，定位 skill-engine 的差异化价值与差距。

---

## 3. 评估对象与范围

| 执行模式 | 子命令 | 定位 | 本次覆盖 |
|---|---|---|---|
| tool_dispatch（基线） | `run --tool-dispatch` / `-td` | 单模型原生循环，普通编码能力基准 | t1–t7 全量（8 维） |
| session（多轮） | `session -s <skill>` | 多轮交互、人机协作 | 方法论文档覆盖 |
| MOA（多模型协作） | `moa --plan <json>` | 多 worker 互审 + commander | t1（文档）/ t2、t6、t7（配对实验） |

**对比基准：** OpenCode（单模型 + Build/Plan 双模式 + 会话持久化）及其 ACP 变体、Claude Code，数据取自同一评测库的 t1/t6 结果。

**未覆盖：** t3/t4/t5 暂无 MOA 配对数据；MOA 的 8 维明细因探针未落库暂缺（见第 7 节局限）。

---

## 4. 评估方法论

### 4.1 整体架构（黑盒驱动）
code-agent-eval 的 `SkillEngineDriver` / `SkillEngineDriverMoa` **从不 import skill-engine 内部代码**，而是：
1. 以 `python -m skill_engine <子命令>` 子进程方式驱动引擎（`cwd` 指向引擎根目录）；
2. 解析 stdout 中的三个契约面：
   - **命令/flag 拼写**（CLI 接口稳定性）；
   - **成本行**：`LLM 调用: N · Token: T (in=X, out=Y)` / MOA `轮次: R · LLM 调用: N · Token: T ...`；
   - **trace 行**：`🔧 bash` / `🔧 <tool>`（工具调用轨迹）。

该设计使评测框架与引擎实现解耦——引擎内部重构不影响评测，只要契约面不变。

### 4.2 指标体系
综合加权分 `weighted_avg` 由 8 个维度加权得到（权重见下表）。同时记录：
- `oracle_pass_rate`：黄金答案（oracle）通过率 —— 衡量"是否真正做对"；
- `task_pass_rate` / `regression_pass_rate`：任务达成率 / 回归不破坏率；
- `tokens_total`（in/out）、`llm_calls`：成本与开销；
- `duration_s`、`turns`：时延与交互轮次。

| 维度 | 权重 | 含义 |
|---|---|---|
| correctness（正确性） | 0.30 | 输出是否满足需求、oracle 通过 |
| verification（验证闭环） | 0.12 | 是否真实运行/验证改动 |
| process（流程保真） | 0.12 | 是否先感知后改、有计划 |
| tooluse（工具使用） | 0.10 | 是否恰当使用专属工具 |
| constraints（约束遵循） | 0.12 | 是否越界改动范围外文件 |
| clarification（澄清） | 0.08 | 歧义时是否主动澄清（N/A 不计） |
| efficiency（效率） | 0.08 | events/turns 效率 |
| robustness（健壮性） | 0.08 | 是否出现错误/危险命令 |

### 4.3 任务分级（T1–T7）
| 档位 | 任务 | 特征 |
|---|---|---|
| T1 | t1_bugfix | 单文件简单 bug 修复 |
| T2 | t2_refactor | 多文件重构 |
| T3 | t3_feature | 新功能实现 |
| T4 | t4_contract | 契约/接口约束 |
| T5 | t5_navigate | 代码导航/定位 |
| T6 | t6_ambiguous | 歧义需求（需求不完整） |
| T7 | t7_comprehensive | 综合大型改动 |

### 4.4 环境与配置
- 统一 LLM 配置：两个模型 profile（`default` 主模型、`secondary` 备选模型，MOA 多模型互审时使用），经统一 gateway 调用。
- 评测约束：每任务单次运行（n=1）；MOA 实验设 1200s 超时上限。
- 数据批次：
  - **批次 A（基线能力画像）**：results/index.json，约 2026-08-14 ~ 08-16，skill-engine 跑完 t1–t7（含 8 维）。
  - **批次 B（MOA 配对实验）**：moa_probe 真机实验，2026-08-17，t2/t6/t7 各跑 基线 + MOA 同实例对照；t1 取自方法论文档。

> **重要说明：** 批次 A 与批次 B 为不同时间、不同随机条件的运行，基线绝对数值存在差异（如 t2 基线：批次 A 0.4487 / 批次 B 0.93）。因此本报告将「基线能力画像」与「MOA 同实例对照」**分开呈现**，避免跨批次混比；MOA 的增益/亏损结论仅基于批次 B 的配对数据。

---

## 5. 评估结果

### 5.1 兼容性复核结论（针对 skill-engine 代码更新）
**结论：code-agent-eval 完全兼容，无需改动。**

逐契约面复核（只读，未修改 skill-engine）：

| 契约面 | 复核点 | 状态 |
|---|---|---|
| CLI 命令/flag | `run`/`session`/`moa` 子命令；`--tool-dispatch/-td`、`-w`、`-s`、`--plan/-p`、`--list-models/-L`、`--args`、`--max-iter`、`-v` 拼写均未变 | ✅ 不变 |
| 成本行格式 | 基线 `LLM 调用:`（cli.py:390）、MOA `轮次:…LLM 调用:…Token:`（cli.py:1268）正则仍可匹配 | ✅ 不变 |
| trace 行 `🔧` | MOA 在 `cli.py:1240-1254` 显式构造 `CliHumanIO` 传入 `MoaOrchestrator`→`ToolDispatchRunner`；基线 `run -td` 对 `human_in_loop` skill 也走 `CliHumanIO`；`CliHumanIO.emit_tool`（human_io.py:155-158）**硬编码 `🔧`** | ✅ 保留 |
| 默认 MOA plan | `code-agent-eval/plans/moa_codebuilder.json` 存在 | ✅ 存在 |

**唯一潜在风险（非阻断）：** `tool_dispatch.py:610` 新增的 `[tool]` 回退分支仅在 `human_io is None` 时触发；而 CLI 子进程驱动永远经 `CliHumanIO` 进入，永不命中该分支。该回退仅影响真正无 human_io 的 headless/Web 调用，对 code-agent-eval 零影响。

### 5.2 基线能力画像（批次 A，skill-engine t1–t7，含 8 维）

| 任务 | weighted_avg | oracle | task_pass | regression | 时延(s) | turns |
|---|---|---|---|---|---|---|
| t1_bugfix | **0.8878** | 1.00 | 1.00 | 1.00 | 45.9 | 2 |
| t2_refactor | 0.4487 | 0.00 | 0.00 | 0.00 | 121.5 | 1 |
| t3_feature | 0.7957 | 1.00 | 1.00 | 1.00 | 271.0 | 7 |
| t4_contract | 0.6913 | 1.00 | 1.00 | 1.00 | 122.5 | 3 |
| t5_navigate | 0.8565 | 1.00 | 1.00 | 1.00 | 321.3 | 2 |
| t6_ambiguous | 0.6378 | 0.75 | 0.00 | 1.00 | 36.1 | 1 |
| t7_comprehensive | 0.8328 | 1.00 | 1.00 | 1.00 | 372.5 | 2 |

**8 维原始分明细（批次 A）：**

| 任务 | corr | ver | proc | tool | cons | clar | eff | rob |
|---|---|---|---|---|---|---|---|---|
| t1 | 1.0 | 1.0 | 0.7 | 1.0 | 0.6 | 1.0 | 0.76 | 1.0 |
| t2 | 0.0 | 0.0 | 0.7 | 1.0 | 0.6 | 1.0 | 0.96 | 1.0 |
| t3 | 1.0 | 1.0 | 0.7 | 1.0 | 0.6 | 1.0 | 0.30 | 0.4 |
| t4 | 1.0 | 0.0 | 0.7 | 1.0 | 0.6 | 1.0 | 0.60 | 0.4 |
| t5 | 1.0 | 1.0 | 0.7 | 1.0 | 0.6 | 1.0 | 0.40 | 1.0 |
| t6 | 0.75 | 0.0 | 0.7 | 1.0 | 0.6 | 0.0 | 0.96 | 1.0 |
| t7 | 1.0 | 1.0 | 0.7 | 1.0 | 0.6 | 1.0 | 0.56 | 0.4 |

**画像解读：**
- **优势维度：** tooluse 全任务 1.0（专属工具使用成熟）、correctness 在多数任务达 1.0、robustness 在非复杂任务达 1.0。
- **主要失分：** t2（重构）correctness/verification 直接 0 —— 多文件重构是基线最大短板；t6（歧义）clarification 0 + verification 0 —— 需求不清时未主动澄清、也未验证；t3/t4/t7 的 robustness 仅 0.4（存在错误/危险命令风险）；verification 在 t2/t4/t6 为 0（未做实跑验证）。

### 5.3 MOA vs 基线 同实例对照（批次 B，配对实验）

| 任务 | 模式 | weighted_avg | oracle | tokens_total | llm_calls | 状态 |
|---|---|---|---|---|---|---|
| **t1** bugfix | 基线 | 0.954 | — | 44,752 | — | 完成 |
| | MOA | 0.670 | — | 96,567 | — | 完成 |
| **t2** refactor | 基线 | 0.930 | 1.0 | 46,851 | 9 | 完成 |
| | MOA | 0.670 | 1.0 | 127,399 | 25 | 完成 |
| **t6** ambiguous | 基线 | 0.555 | **0.0** | 178,804 | 30 | 完成 |
| | MOA | **0.616** | **1.0** | 136,163 | 22 | 完成 |
| **t7** comprehensive | 基线 | 0.828 | 1.0 | 228,873 | 36 | 完成 |
| | MOA | 0.568 | 1.0 | — | 0 | **超时(1200s)** |

**对照结论：**
- **t1（简单）：** MOA 综合分 −0.284，token **+116%**。多视角在"改一行"任务上是纯负优化。
- **t2（重构）：** MOA 综合分 −0.26，token **+172%**，llm_calls 2.8×。多模型互审反而拉低重构质量、翻倍成本。
- **t6（歧义）：MOA 翻盘。** 综合分 +0.061（幅度小但关键），oracle 从 **0.0 → 1.0**（基线完全做错，MOA 做对），且 token **−24%**、calls 0.73×。这是 MOA 价值最确凿的证据——多方协作有效消解了需求歧义。
- **t7（综合）：** MOA **直接超时**，基线 0.828 完胜。MOA 多轮开销在超综合任务上把自己拖死。

**成本效率（token / weighted_avg，越低越省）：**
| 任务 | 基线 token/分 | MOA token/分 |
|---|---|---|
| t2 | ~50,400 | ~190,100（3.8× 更费） |
| t6 | ~322,200 | ~221,000（更省且更高分） |
| t7 | ~276,400 | 超时（不可用） |

### 5.4 关键发现：MOA 翻盘条件
MOA **没有在"难任务"上普遍翻盘**，其增益高度依赖任务类型：
- ✅ **歧义 / 澄清类（t6）：确定性增益** —— 多模型互审弥补了单模型"不敢问/问错"的短板，oracle 从 0 到 1。
- ❌ **重构类（t2）：确定性亏损** —— 单模型已能正确处理，多模型徒增协调成本。
- ❌ **超综合类（t7）：不可用** —— 多轮预算溢出导致超时。
- ❌ **简单类（t1）：确定性亏损** —— 成本翻倍、质量下降。

### 5.5 与 OpenCode 横向对比（批次 A，t1/t6）

| Agent | t1 weighted_avg | t1 oracle | t6 weighted_avg | t6 oracle |
|---|---|---|---|---|
| skill-engine（基线） | 0.8878 | 1.0 | 0.6378 | 0.75 |
| skill-engine（MOA，批次 B） | 0.670 | — | 0.616 | 1.0 |
| OpenCode | 0.8304 | 1.0 | 0.629 | 0.75 |
| OpenCode-ACP | **0.9522** | 1.0 | **0.9128** | 1.0 |
| Claude Code | 0.8304 | 1.0 | 0.844 | 1.0 |

**对比解读：**
- OpenCode-ACP 在 t1/t6 均显著领先（0.95/0.91），说明**单模型 + 强工具链的成熟方案**在多数场景仍是最优性价比。
- skill-engine 基线在 t1 接近 OpenCode（0.89 vs 0.83），但在 t6 歧义任务偏弱（0.64 vs OpenCode 0.63、ACP 0.91）。
- skill-engine 的**差异化价值**不在绝对分数，而在：① 工程化成本埋点（每次执行直接输出 token，可被评测自动抓取）；② 可选的 MOA 多模型协作（在歧义场景可拉齐甚至超越单模型基线）。

---

## 6. 风险、局限与偏差

1. **样本量小：** 每任务仅单次运行（n=1），结论存在随机波动；建议关键任务（尤其 t6 翻盘结论）做 3–5 次重复取均值。
2. **批次差异：** 基线在批次 A/B 数值不一致（见 4.4），MOA 增益结论依赖配对批次 B，绝对分不可跨批次比较。
3. **MOA 8 维缺失：** moa_probe 汇总中 8 维明细全为 0（探针未落库），MOA 的维度级归因暂不可得，仅能看综合分 + oracle。
4. **覆盖率缺口：** t3/t4/t5 无 MOA 配对数据，MOA 在这些档位的损益未知。
5. **t7 超时：** 1200s 上限下 MOA 未完成，实为"不可用"而非"做错"，但其多轮开销风险已被证实。
6. **模型 vendor 未固定披露：** 本报告以 profile 名（`default`/`secondary`）指代模型，具体厂商/版本随配置变化，跨配置复现时成本绝对值会有差异。

---

## 7. 结论与选型建议

### 7.1 总体结论
- skill-engine 基线编码能力**达到可用水平**（多数任务 oracle 1.0），短板集中在**多文件重构（t2）**与**歧义需求（t6）的澄清/验证环节**。
- MOA 是**条件增益工具**而非万能升级：仅在**需求歧义 / 需多视角校验**场景创造确定性价值；在其余场景系统性地降分、增本、甚至超时。
- 评测框架兼容性稳固，可放心用于 skill-engine 的持续回归评测。

### 7.2 MOA 选型决策矩阵

| 任务特征 | 是否建议启用 MOA | 依据 |
|---|---|---|
| 单文件简单 bugfix / 微小改动 | **否** | t1：MOA 0.670 vs 0.954，token +116% |
| 多文件重构 | **否（当前）** | t2：MOA 0.67 vs 0.93，token 2.7× |
| 需求歧义 / 信息不全 | **是** | t6：MOA oracle 1.0 vs 0.0，且 token 更少 |
| 综合大型改动 | **谨慎 / 否** | t7：MOA 超时，基线 0.828 |
| 通用默认 | **否** | 成本不划算，基线性价比更优 |

### 7.3 行动建议
1. **默认走基线模式**，将 MOA 作为"歧义任务专用开关"，而非全局默认。
2. **补强基线短板：** 在 t2（重构）增加"先读全貌再改"的引导；在 t6（歧义）强制澄清前置（clarification 维度当前 0 分）。
3. **提升 verification 覆盖率：** t2/t4/t6 的 verification 为 0，应强制实跑验证后再交付。
4. **扩充 MOA 评测：** 补齐 t3/t4/t5 配对数据，并对 t6 做多次重复以确认翻盘稳健性；为 moa_probe 补上 8 维落库。
5. **MOA 防超时：** 对 t7 类综合任务设定更低 max_rounds 或预拆分子任务，避免 1200s 预算溢出。

---

## 8. 附录

### 8.1 数据来源索引（本评估包文件清单）
本报告所有原始数据已随包提供，目录结构如下：

```
评估/
├── 00_企业级评估报告/                  ← 本报告
├── 01_MOA实证数据(moa_probe)/         ← 批次 B 配对实验原始数据
│   ├── moa_probe_summary.json         ← t2/t6/t7 汇总（weighted_avg/oracle/token/calls）
│   ├── t2_refactor/  t6_ambiguous/  t7_comprehensive/
│   │   ├── baseline_skill-engine_result.json   ← 基线单次运行明细（duration/turns/file_edits/events/tokens）
│   │   ├── moa_skill-engine-moa_result.json    ← MOA 单次运行明细
│   │   └── 产物样本/                  ← Agent 实际产出的代码文件
├── 02_基线评测库(results)/            ← 批次 A 全量评测库
│   ├── index.json                     ← 37 条记录，含 8 维明细（核心数据源）
│   ├── baseline_coding.json
│   └── skill-engine/coding/t1..t7/   ← 各任务 score.json
├── 03_对比文档与方法论/
│   ├── 执行模式使用说明.md            ← 三模式使用 + OpenCode 对比 + MOA 实证方法
│   └── SPEC.md                        ← code-agent-eval 评测框架设计规格
├── 04_历史评估报告(reports)/
│   ├── opencode_acp_suite_2026-08-16.md
│   ├── opencode_vs_skillengine_2026-08-16.md
│   └── opencode_full_2026-08-15.md
└── 05_评估脚本与配置/
    ├── moa_probe.py                   ← MOA 配对探针
    ├── moa_vs_session.py              ← MOA vs Session 批量对比脚本
    ├── validate_v3.py                 ← v3 成本埋点校验
    └── moa_codebuilder.json           ← 默认 MOA plan 配置
```

### 8.2 默认 MOA plan 配置示例（moa_codebuilder.json）
采用 `model_profile` / `skill_name` / `instruction` 字段，含若干 worker（A1 default/code-builder、A2 secondary/code-builder）+ commander（C default/moa-commander）+ options（max_rounds / max_agent_iterations / max_llm_calls）。详见 `05_评估脚本与配置/moa_codebuilder.json`。

### 8.3 术语表
- **weighted_avg：** 8 维加权综合分（0–1），本评测主指标。
- **oracle_pass_rate：** 黄金答案通过率，衡量"是否真正做对"。
- **tool_dispatch：** skill-engine 档位 B 原生工具循环，对应普通编码能力。
- **MOA：** Mixture of Agents，多 worker 模型互审 + commander 决策的多模型协作架构。
- **driver 契约面：** 评测框架与引擎之间仅通过 CLI 拼写、成本行、trace 行三类 stdout 接口耦合。

---

*报告完。数据可溯源至 `01_~05_` 各目录原始文件；兼容性复核为只读验证，未对 skill-engine 做任何改动。*
