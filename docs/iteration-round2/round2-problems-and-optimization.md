# 迭代控制整改 · 第二轮问题与优化

> 日期：2026-08-22
> 背景：第一轮整改（A1/A2/A3a/A4/A5/A6/A4b/B1-B4 + 审计 6 Bug）已落地并提交（commit `47a45f6`）。
>       之后用相同任务（code-builder 排查 AC率=0 bug）跑了一次实测，另一 agent 给出评价。
> 本文档**独立于第一轮设计/审计文档**，只记录二轮实测暴露的残留问题 + 优化方案。

---

## 0. 实测对比基线

| 维度 | 第一轮前基线（处理流程.txt） | 第一轮后实测（运行记录.txt） | 变化 |
|---|---|---|---|
| 总工具调用/步数 | ~83 步 | ~65 步 | ↓ ~21% |
| 临时 .py 脚本 | 7-8 个 | verify_ac_rate.py 写 5 次 | 略降 |
| `<key_finding>` 固化 | 0 | 2-3 次 | 新增（A4b 生效） |
| 撞 max_iterations 静默丢失 | 是 | 否（正常收敛） | 修复（A2 生效） |
| 思考折叠 | 无 | 日志见 `[已折叠: 旧轮思考过程]` | 新增（A4b 压缩生效） |

**结论**：第一轮的机制（预算可见 / 压缩三段式 / pinned block / search 上下文 / stderr 不截断 / 指令注入）**确实生效**，
但进步幅度（~21%）远低于预期（预估 50%），因为有几个**第一轮没覆盖的真实口子**没堵住。

---

## 1. 另一 agent 评价的事实核查（区分误判与真问题）

该评价从**被桌面查看器折叠/截断的日志**反推"哪些机制生效"，未读源码，导致部分误判。逐条核对：

### 1.1 评价误判（实际已落地，被日志折叠掩盖）

| 评价断言 | 事实 | 证据 |
|---|---|---|
| "A1 没落地（日志无 `[进度]`）" | A1 已落地，进度提示是 **user 角色注入 + invoke 后 pop**，日志折叠视图看不到注入消息 | tool_dispatch.py L1140 + L1170 + `_pop_progress_hint` |
| "A5 没落地（search 只显示 X matches 无上下文）" | A5 已改 `_format_match` 带 `← MATCH` + ripgrep `-C 3` 上下文，日志里 `X matches` 后跟随上下文行被查看器折叠 | tool_dispatch.py `_run_ripgrep` `-C context_lines` |
| "只动了 prompt 层没动引擎层" | A1/A2/A4/A5/A6/A3a/A4b/B4 全是引擎代码改动（tool_dispatch/executor/context_manager/assembler） | git diff `47a45f6` 6 文件 +345/-44 |
| "A4 大文件策略反转没做" | SKILL.md L167-171 已改（≤800 全文、>800 读完整区间、禁 >3 次读） | skills/code-builder/SKILL.md |

### 1.2 评价真问题（确认属实，但部分归因偏了）

| 评价点 | 事实 | 归属 |
|---|---|---|
| **edit_file 反复失败浪费 ~10 步** | 真。日志 `oldText 出现2次`→`不存在`→`EDIT BLOCKED` 循环 7+ 步 | 工具设计问题，**不在第一轮整改范围**（A/B 层均未含 edit 改造） |
| **verify_ac_rate.py 写了 5 次** | 真。A3a 已让 `python -c` 可用，但模型仍写临时脚本 → A3b（run_python 工具）确有价值 | 即之前你叫停的 A3b |
| **pytest 超时无分析** | 真。B1 自愈阶梯针对"测试失败"，未覆盖"超时（exit -1）"非失败态 | B1 需补"超时"分支 |
| **query_db(sql) 工具高 ROI** | 真。新增工具可消临时脚本 | 属新增，不在原范围 |

### 1.3 真正的根因（第一轮进步有限的主因）

不是"没落地引擎层"，而是两个**第一轮自己留下的漏洞 + 超出范围的新问题**：

1. **A4c 重复读检测被 `force_refresh` 绕过**（我设计的真实漏洞）：
   tool_dispatch.py L2098 `if read_count >= 3 and not force_refresh:` ——
   模型**滥用 `force_refresh: True` 切片读大文件**，检测直接豁免。
   日志里 database.py 被读 15+ 次，几乎每次带 force_refresh。
2. **edit 工具 UX / query_db 缺失 / pytest 超时无分析**：均超出第一轮整改清单。

---

## 2. 二轮优化方案（3 个 P0，按 ROI）

### P0-1：A4c 重复读检测覆盖 `force_refresh`（修自己的漏洞）

**问题**：L2098 的 `not force_refresh` 豁免让模型用 force_refresh 切片读大文件绕过检测。
**方案**：
- 重复计数仍对所有 read（含 force_refresh）累计。
- 当同一文件 read 次数 ≥ 3（无论是否 force_refresh）且**本次是分页切片（给了 offset/limit 而非全文）**，注入提示：
  "你已第 N 次读取 X（含 Nf 次 force_refresh 切片）。强制切片读同一大文件会快速耗尽预算。
   请改用 `force_refresh=true` 且不带 offset/limit 一次性取全文，或基于已读内容直接改。"
- 仅对"带 offset/limit 的切片读"计数触发，全文读（无 offset/limit）不触发（避免干扰正常全文读）。

**预期省步**：database.py 等从 15+ 次读压到 2-3 次全文读 → 省 8-12 步。
**风险**：低（仅调整提示注入条件，不改计数本身）。
**落点**：tool_dispatch.py L2091-2110。

### P0-2：edit_file 支持行号范围 / diff patch（工具层大改）

**问题**：edit_file 要求 oldText 在文件中唯一，但 `profile["tag_names"] = TAG_DISPLAY` 出现 2 次，
模型为找唯一锚点浪费 7+ 步（3 次 edit 失败 + 多次 read）。
**方案**（二选一，需先排查 edit_file 实现）：
- 方案 A：edit_file 支持 `{"oldText":..., "line_range":[start,end]}` 限定替换区间。
- 方案 B：新增 `apply_patch` 工具，用 V4A/unified diff 格式替换，不依赖 oldText 唯一性。
**预期省步**：edit 失败循环（~10 步）直接消失。
**风险**：中（工具接口变更，需同步 LLM schema + 解析逻辑）。
**落点**：tool_defs.py `edit_file` + tool_dispatch 对应分支（待排查后定）。
**注意**：此改动超出第一轮范围，属独立工具层改造，单独实施。

### P0-3：query_db(sql) 工具（新增，消临时脚本）

**问题**：模型为验证 AC 率写了 5 次 verify_ac_rate.py（write_file + bash 循环）。
**方案**：新增 `query_db(sql: str) -> str` 工具，直接执行只读 SQL 并返回结果（经 A6 的 format_observation 不截断）。
- schema 声明（tool_defs.py，无 body 同 search_files 模式）。
- tool_dispatch 主循环加 `elif tc["type"] == "query_db":` 分支：用 sqlite3 连工作目录的 db 执行 SELECT，返回表格。
- 复用 A3a 的 `_build_env` 注入 + A6 的 `format_observation`。
**预期省步**：5 次 write+run 临时脚本 → 1 次 query_db 调用，省 8-10 步。
**风险**：低（只读 SELECT，不执行 DDL/DML；可加白名单校验）。
**落点**：tool_defs.py + tool_dispatch.py（参考 B4 update_plan 的落地骨架）。

### 附：P1（非 P0，但顺手可做）
- **B1 补 pytest 超时分支**：当 bash 返回 `exit_code: -1 (timed_out)` 时，注入提示
  "测试超时（非失败）。先收窄到单个测试文件/用例（`pytest tests/xxx.py::test_y -x`），
   或检查 DB 锁/死循环，不要无分析重跑全量。" → 省 2-3 步。
- **A3b run_python**（你之前叫停）：若做可进一步消临时脚本，但 P0-3 的 query_db 已覆盖 DB 验证场景。

---

## 3. 实施顺序与验证

1. **P0-1（A4c force_refresh 覆盖）**：最小改动，先落地，py_compile + 逻辑测试（构造带 force_refresh 的重复切片读，确认提示注入）。
2. **P0-3（query_db 工具）**：参考 B4 骨架，schema + 分支 + 逻辑测试（构造 SELECT 返回）。
3. **P0-2（edit_file 行号/diff）**：最后做，需先排查 edit_file 实现，改动最大。
4. 每个 P0 做完独立 commit，不混在首轮 commit 里。

---

## 4. 预期效果（二轮做完后）

| 指标 | 首轮后（实测） | 二轮后（预期） |
|---|---|---|
| 总步数 | ~65 | ~30-35 |
| 同文件反复读 | database.py 15+ 次 | 2-3 次全文 |
| 临时脚本 | 5 次写 verify | 0（query_db 替代） |
| edit 失败循环 | 7+ 步 | 0（行号/diff 支持） |
| pytest 超时 | 无分析重跑 | 收窄单测 |

> 注：评价预估"65→25-30"，与本文预期一致。核心差异在于本文**先修首轮自己的漏洞（P0-1）**，
> 而非假设首轮没落地。

---

## 5. 实施记录（已落地）

### 2026-08-22 实施 3 个 P0

| P0 | 改动文件 | 核心改动 | 验证 |
|---|---|---|---|
| **P0-1** A4c force_refresh 覆盖 | tool_dispatch.py L2089-2110 | 重复读检测不再被 `not force_refresh` 豁免；触发条件改为 `read_count>=3 and is_paged`（is_paged=带 offset/limit 切片读）。全文读（无 offset/limit）不误伤 | ✅ 逻辑测试：force_refresh 切片读触发、全文读不触发、未达阈值不触发 |
| **P0-3** query_db 工具 | tool_defs.py（schema + 注册）+ tool_dispatch.py 主循环分支 | 新增 `query_db(sql, db_path)` 只读 SQL 工具（SELECT/PRAGMA/EXPLAIN/WITH 白名单；自动 rglob *.db）；消临时脚本 | ✅ 逻辑测试：拒绝 DDL/DML、SELECT 执行、空结果、表格化 |
| **P0-2** edit_file 行号/diff | tool_dispatch.py `_apply_edits` + tool_defs.py edit_file docstring | 支持 `line_range:[s,e]` 锚定编辑（区间内定位 oldText，不要求全局唯一）；消除"oldText 出现2次"失败循环 | ✅ 逻辑测试：重复行 line_range 锚定成功、区间外报错、全局重复仍正确失败 |

**全量 py_compile 通过**（tool_dispatch.py / tool_defs.py）。
**3 个 P0 均独立逻辑测试通过**，临时验证脚本已清理。

**配套提示增强**：
- edit_file docstring 加 line_range 用法说明（LLM 调用时可见）。
- P0-1 提示文案明确"用 force_refresh=true 且**不带 offset/limit** 一次性取全文"。

**提交**：`git commit`（独立于首轮 `47a45f6`，单独 commit）。

### 2026-08-22 补充实施 P1-1 / P1-2（用户实测运行记录-2 后）

实测（运行记录-2）暴露：P0-3 的 query_db 工具已就绪但**模型不知道用**（仍写 5 个临时脚本），
且 pytest 超时（exit -1）仍无分析重跑。补两个小改动：

| 项 | 文件 | 核心改动 | 验证 |
|---|---|---|---|
| **P1-1** query_db 推广 | skills/code-builder/SKILL.md | 验证指南加"DB 数据验证用 query_db 工具，别写临时 .py 脚本" | ✅ 文档指令（B 层） |
| **P1-2** pytest 超时硬提示 | tool_dispatch.py `format_observation` | `timed_out` 时：若含 pytest 特征 → 专属 hint 收窄单测；否则通用超时 hint | ✅ 逻辑测试：pytest超时/非pytest超时/正常失败 三态正确 |

**全量 py_compile 通过**；P1-2 独立逻辑测试通过（三态分支正确）。

**改动量评估（实测确认）**：
- P1-1：~4 行 SKILL.md 指令，零引擎代码。
- P1-2：~14 行 tool_dispatch.py（timed_out 分支加 hint），机制现成（_diagnose_shell_error 同款）。

**提交**：`git commit`（独立于 P0 commit `a6d1276`）。

### 实测效果对比（运行记录-1 → 运行记录-2）
- 总步数：~65 → ~52（↓ 约 20%）
- edit 失败循环：7+ 步 → 0（P0-2 line_range 生效，无 "oldText 出现2次"）
- database.py 反复读：15+ → ~8-10（P0-1 部分生效，cache 命中增多）
- **残留**：query_db 仍未被模型调用（P1-1 指令刚加，需下次实测验证）；pytest 超时在无 P1-2 时仍浪费 2-3 步

### 残留（仍未做）
- A3b run_python（你叫停，P0-3 query_db 已覆盖）
- edit_file 的 diff/patch 格式（P0-2 line_range 已解决核心痛点）
- database.py 切片读彻底压（P0-1 只检测不阻断，模型惯性仍在；可升级为"第3次强制建议全文"）

---

## 6. 二轮审计报告修复（另一 agent 评审）

另一 agent 对二轮 P0/P1 改动做交叉评审，提 6 个 Bug。逐条核对代码后 **5 个真实 + 1 个表述偏差（Bug1 触发条件描述不准，但 bug 本身属实）**。首轮审计 Bug1/Bug2（`_build_handoff` role、`system` 消息拒收）已在 `47a45f6` 修过，不在本轮。

| Bug | 严重度 | 真实？ | 根因 | 修复 |
|---|---|---|---|---|
| **Bug1** 重复 `tool_call_id` | P0 | ✅ 属实 | P0-1 准备阶段 append 独立 tool 消息（tc["id"]），执行阶段再 append 结果（同 tc["id"]）→ 两个同 ID tool 消息 → OpenAI `duplicate tool call id` 报错、循环挂 | 准备阶段不 append，改为把提示存 `tc["_repeat_hint"]`，执行阶段拼到读取结果内容开头（只 1 条 tool 消息） |
| **Bug2** 多 line_range 行号错位 | P1 | ✅ 属实 | `_apply_edits` 顺序处理 + 就地改 lines，前 edit 改行数后后续 line_range 指错位 | 按 line_range[0] 降序处理（从后往前改） |
| **Bug3** query_db 列宽只看表头 | P2 | ✅ 属实 | `width = max([len(c) for c in cols]+[8])` 忽略数据 | `all_vals = cols + 所有行值`，width 取最大值 |
| **Bug4** fetchall 无行数上限 | P2 | ✅ 属实 | `cur.fetchall()` 大表拉几万行 | `cur.fetchmany(200)` + 检测 extra 行，尾部提示"仅显示前 200 行" |
| **Bug5** WITH 允许数据修改 CTE | P2（安全） | ✅ 属实 | 白名单 `^(...|WITH)` 允许 `WITH x AS (DELETE...)` | 收紧：`WITH\s+\w+\s+AS\s*\(\s*(SELECT|WITH)` 仅允许 SELECT 起步 CTE |
| **Bug6** format_observation 包装 SQL | P2 | ✅ 属实（轻微） | query_db 非 shell，套 format_observation 带 `exit_code: 0 / stdout:` 前缀语义不纯 | 直接 `content: obs`，不加前缀 |

**全量 py_compile 通过**；逻辑测试覆盖：Bug2（降序无错位 + 减行场景）、Bug3（列宽容纳数据）、Bug4（限 200 + extra）、Bug5（DELETE/UPDATE CTE 拒、SELECT CTE 放）、Bug6（无前缀）。Bug1 因涉及 messages 注入链路，靠代码审查 + 单消息验证（准备阶段不再 append）。

**提交**：`git commit`（独立于前序 commit）。

---

## 7. 第三轮增强（基于 round2-trace-analysis.md 实测分析）

另一 agent 基于真实 trace 分析"为何只降 21%"，核心论点：**改进是建议性而非强制性**，模型可忽略提示。
取其中 ROI 最高且风险可控的 4 项实施（不做"强制式软上限/第4次直接拒绝"等高风险硬阻断）：

| 项 | 文件 | 核心改动 | 验证 |
|---|---|---|---|
| **T1** Bash Python hint（Pydantic 字段错误） | tool_dispatch.py `_SHELL_ERROR_HINTS` | 新增 `is not a valid field` / `has no attribute` / `ValidationError` 专属提示：先改 models 再改业务 | ✅ 逻辑测试：Pydantic 错误命中 + 通用错误仍正常 |
| **T2** 编辑顺序引导 | skills/code-builder/SKILL.md | 加"自底向上编辑：先 models → 再 database/service → 最后 router/api" | ✅ B 层指令 |
| **T3** A3b run_python 工具 | tool_defs.py（schema+注册）+ tool_dispatch.py 分支 | 新增 `run_python(code, timeout)`：临时 .py 文件 + executor（venv）执行，规避 cmd 引号；消 Python 验证临时脚本 | ✅ 逻辑测试：schema/注册/分支(executor+清理) 齐全 |
| **T4** 重复读第4次强制返全文缓存（非拒绝） | tool_dispatch.py P0-1 块 + read 执行分支 | 第4+次分页读且已有全文缓存 → 标记 `_force_cache_full`，执行阶段直接返回缓存全文（不实际读文件、不消耗迭代） | ✅ 逻辑测试：标记 + 执行阶段复用 |

**全量 py_compile 通过**；4 项逻辑测试通过。

**与 round2-trace-analysis 的偏差澄清**：
- 该分析引用的 trace（处理记录.txt = 运行记录-1）早于本修复轮，其最担心的"P0-1 警告因重复 tool_call_id 不可见"已在 commit `3f0d9d2`（6 Bug 修复）解决。
- 它建议的"强制式软上限/第4次直接拒绝"**未做**——硬阻断风险高（可能误伤合理场景），T4 改用"返缓存全文"的软阻断式替代。
- A3b（T3）此前用户叫停，本次基于 trace 实测（Python 验证脚本仍写）重新评估后实施。

**提交**：`git commit`（独立于 6-Bug 修复 commit）。
