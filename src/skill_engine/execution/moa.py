"""MOA — Mixture of Agents 多模型 / 多 skill 协作编排器

背景与定位
----------
skill-engine 原本的执行核心是单一的 ``ToolDispatchRunner.run(skill, llm)``：
一个 skill + 一个模型，串行 agent loop。MOA 在其之上做"编排层"，把 N 个
``(模型 profile × skill × 任务指示)`` 组合成一组 **worker agent**，再由一个
**commander agent** 在每一轮决定"下一个该谁上、干什么"，形成「指挥官驱动」的
协作回路。

两种用户场景在代码层面完全统一，区别只在配置：
- 模式 A（多 skill 协作）：A1/A2/A3 各挂不同 skill（如 VLM 审查 + 代码开发）。
- 模式 B（多模型同 skill 互审）：A1/A2/A3 挂同一 skill、不同模型，互相讨论监督。
两者都只是 worker 的 (model_profile, skill_name) 组合不同，编排逻辑一致。

为什么不用子进程
----------------
每个 worker 的执行**复用进程内**的同一个 ``Executor``（共享 working_root）、
同一个 ``FileSnapshot`` 与 ``FileStateTracker``。这样：
1. A1 写出的文件 A2 立即可见、文件状态跟踪跨 agent 有效、整轮 MOA 可一键回滚；
2. 黑板（blackboard）作为共享上下文在进程内直接传递，零序列化 / 零 IPC；
3. commander 驱动的流程本质是**串行**的（每轮只派一个 agent），没有并行收益，
   因此不需要为"并行"付出子进程的冷启动 + 上下文传输成本。

防死循环（四道闸）
------------------
1. ``max_rounds``：commander 决策轮数硬上限（默认 8）。
2. ``max_agent_iterations``：单个 worker 内层 tool_dispatch 迭代上限（默认 12）。
3. ``max_llm_calls``：全局 LLM 调用次数上限（``CountingLLM`` 计数，默认 60）。
4. 反震荡强制停止：同一 agent 连续命中 ``max_consecutive_same_agent``(默认 3)
   次且黑板无变化 → 强制 STOP，避免 commander 在两个 agent 间无限乒乓。

 commander 决策解析失败时**默认 STOP**（fail-safe），绝不因解析异常而继续空转。
"""

import os
import re
import json
import logging
from pathlib import Path
from typing import Optional

from skill_engine.models import MoaAgent, MatchResult

logger = logging.getLogger(__name__)
from skill_engine.execution.tool_dispatch import ToolDispatchRunner
from skill_engine.execution.tool_defs import parse_named_params
from skill_engine.execution.snapshot import FileSnapshot
from skill_engine.execution.file_tracker import FileStateTracker
from skill_engine.execution.paths import to_native_path


# ── 全局 LLM 调用计数包装（成本闸门 #3） ────────────────────────────────────
# 抽成共享模块，供 moa 与 run 命令（baseline run -td 埋 token）共用同一套机制。
from .counting_llm import CountingLLM, _accumulate_tokens


# ── 共享黑板 / 会话状态 ────────────────────────────────────────────────────
class MoaSession:
    """一次 MOA 运行的共享状态持有者（与 session 模式的 SkillSession 同思路）。

    - snapshot / file_tracker：跨 worker 共享，使文件写读互相可见、整轮可回滚。
    - blackboard：累积每个 worker 的产出（截断后），作为指挥官与下一 agent 的
      共享上下文。
    - 计数器与防震荡字段：支撑四道防死循环闸。
    """

    def __init__(self, working_root: str):
        base = Path(working_root) if working_root else Path.cwd()
        self.working_root = str(base)
        self.snapshot = FileSnapshot(base)
        self.file_tracker = FileStateTracker(strict=False)
        self.blackboard: list[dict] = []   # {"alias","skill","output","files"}
        self.round = 0
        self.llm_calls = 0                  # 经 CountingLLM 累计
        # 防震荡
        self.last_agent_alias: Optional[str] = None
        self.same_agent_streak = 0
        self.prev_blackboard_hash: Optional[int] = None
        self.no_progress_streak = 0

    def add_entry(self, alias: str, skill: str, output: str, files: list[str]) -> None:
        self.blackboard.append({
            "alias": alias,
            "skill": skill or "（内置）",
            "output": output or "",
            "files": list(files or []),
        })

    def last_entry_hash(self) -> int:
        """最近一笔产出的指纹，用于「无进展」检测。

        只取**最新一笔**（增量），而非整块黑板——否则每轮新增一条 entry 都会
        让整体 hash 变化，导致「连续同 agent 产出相同」永远判不出「无进展」。
        指纹含 alias + 输出正文 + 首个文件，比仅用长度更抗碰撞。
        """
        if not self.blackboard:
            return 0
        e = self.blackboard[-1]
        payload = f"{e['alias']}|{e['output']}|{(e['files'] or [''])[0]}"
        return hash(payload)

    def blackboard_summary(self, max_each: int = 1500) -> str:
        """生成供注入 prompt 的共享状态摘要（截断，控制上下文体积）。"""
        if not self.blackboard:
            return "（尚无任何 agent 产出）"
        lines = []
        for i, e in enumerate(self.blackboard, 1):
            out = e["output"] or "（无文本产出）"
            if len(out) > max_each:
                out = out[:max_each] + f" …(截断，共 {len(e['output'])} 字)"
            files = ("；文件: " + ", ".join(e["files"])) if e.get("files") else ""
            lines.append(f"### 第 {i} 步 · {e['alias']}（skill={e['skill']}）\n{out}{files}")
        return "\n\n".join(lines)


# ── 编排器 ────────────────────────────────────────────────────────────────
class MoaOrchestrator:
    """MOA 指挥官驱动的协作回路。

    用法：
    >>> orch = MoaOrchestrator(executor, assembler, approval_fn, human_io, wr)
    >>> result = orch.run(agents, commander, query="优化登录页")
    """

    def __init__(
        self,
        executor,
        assembler,
        approval_fn=None,
        human_io=None,
        working_root: Optional[str] = None,
        plain_text: bool = False,
        verbose: bool = False,
    ):
        self.executor = executor
        self.assembler = assembler
        self.approval_fn = approval_fn
        self.human_io = human_io
        self.working_root = str(to_native_path(working_root) or Path.cwd())
        self.plain_text = plain_text
        self.verbose = verbose
        # verbose 时把引擎内部诊断（决策解析、闸触发、worker 起止）打到 stderr，
        # 不影响 _emit 的用户进度通道；无 handler 时挂一个一次性 StreamHandler。
        if verbose:
            if not logger.handlers:
                _h = logging.StreamHandler()
                _h.setFormatter(logging.Formatter("[moa-debug] %(message)s"))
                logger.addHandler(_h)
            logger.setLevel(logging.DEBUG)

    # ---- 输出通道（有 human_io 走语义通道，否则回退 print） ----
    def _emit(self, text: str) -> None:
        m = getattr(self.human_io, "emit", None)
        if callable(m):
            m(text)
        else:
            print(f"\n[MOA] {text}")

    def _emit_header(self, title: str) -> None:
        m = getattr(self.human_io, "emit_header", None)
        if callable(m):
            m(title)
        else:
            print(f"\n# {title}")

    # ---- commander 决策 ----
    def _commander_prompt(self, commander: MoaAgent, session: MoaSession,
                          agents: list[MoaAgent], query: str,
                          round_no: int, max_rounds: int) -> str:
        roster = "\n".join(
            f"- {a.alias}（{'指挥官' if a.role=='commander' else 'worker'}）："
            f"模型={a.model_profile}，skill={a.skill_name or '内置'}，"
            f"职责={a.instruction or '（未指定）'}"
            for a in agents
        )
        aliases = " / ".join(a.alias for a in agents)
        skill_ctx = ""
        if commander.skill_name:
            try:
                skill = self._registry.load_skill(commander.skill_name)
                if skill:
                    # 复用 assembler 产出该 skill 的指令正文，作为指挥官的人设上下文
                    from skill_engine.execution.tool_dispatch import build_env_header
                    base_dir = Path(self.working_root)
                    skill_ctx = (
                        "\n\n# 指挥官 Skill 指令（你的人设 / 约束）\n"
                        + build_env_header(base_dir, getattr(self.executor, "shell", ""))
                        + self.assembler.assemble(skill, {"$ARGUMENTS": commander.instruction})
                    )
            except Exception:
                skill_ctx = ""
        return f"""\
你是一个多智能体协作任务的**指挥官（commander）**。你不直接写代码或改文件，
你的唯一职责是：根据当前进展，决定**下一轮由哪个 worker agent 行动、干什么**。

# 原始任务
{query}

# 可用 worker agent 名册
{roster}

# 当前协作进度（黑板，含各 agent 的历史产出）
{session.blackboard_summary()}

# 决策要求
- 你正处在第 {round_no}/{max_rounds} 轮。
- 只能在以下代号中选一个作为 next：{aliases}，或输出 STOP 表示任务已完成。
- 若选某个 worker，必须在 task 中写清**本轮具体要它做什么**（结合黑板现状，
  避免重复已经做过的事）。
- 当任务已达成、或继续下去无收益时，果断输出 STOP。
{skill_ctx}

# 输出格式（必须严格遵守，便于程序解析）
把你的决策放在如下围栏内，其余分析可写在围栏外：
<moa_decision>
{{"next": "A1 或 A2 或 A3 或 STOP", "task": "本轮指派给该 agent 的具体任务", "rationale": "你的决策理由"}}
</moa_decision>
"""

    def _parse_decision(self, text: str, agents: list[MoaAgent]) -> dict:
        """从 commander 输出中解析决策。

        解析失败 / 非法 agent / 命中 STOP 关键词 → 返回 {{"next": "STOP"}}（fail-safe）。
        """
        if not text:
            return {"next": "STOP", "task": "", "rationale": "（commander 无输出）"}
        m = re.search(r"<moa_decision>(.*?)</moa_decision>", text, re.DOTALL)
        if m:
            # 取围栏内全部内容再 json.loads，避免因 rationale 含花括号导致
            # 非贪婪 \{.*?\} 误截。解析失败一律安全 STOP。
            try:
                data = json.loads(m.group(1).strip())
            except Exception:
                return {"next": "STOP", "task": "", "rationale": "（JSON 解析失败，安全停止）"}
        else:
            # 容错：无围栏时检测 STOP 关键词
            if re.search(r"\b(STOP|停止|结束任务|完成)\b", text, re.IGNORECASE):
                return {"next": "STOP", "task": "", "rationale": "（关键词 STOP）"}
            return {"next": "STOP", "task": "", "rationale": "（决策块解析失败，安全停止）"}
        nxt = str(data.get("next", "")).strip().upper()
        valid = {a.alias.upper(): a.alias for a in agents}
        if nxt in ("STOP", "结束", "完成", "DONE"):
            return {"next": "STOP", "task": str(data.get("task", "")),
                    "rationale": str(data.get("rationale", ""))}
        if nxt in valid:
            return {"next": valid[nxt], "task": str(data.get("task", "")),
                    "rationale": str(data.get("rationale", ""))}
        return {"next": "STOP", "task": "", "rationale": f"（未知 agent 代号 {nxt}，安全停止）"}

    # ---- worker 执行 ----
    def _run_agent(self, agent: MoaAgent, session: MoaSession, task: str,
                   query: str, round_no: int, max_agent_iterations: int) -> tuple[str, list[str]]:
        """执行单个 worker：组装 (model × skill × 指令+黑板) 并跑一次 tool_dispatch。"""
        if agent.skill_name:
            skill = self._registry.load_skill(agent.skill_name)
            if not skill:
                return f"[ERROR] 无法加载 worker skill: {agent.skill_name}", []
        else:
            # 内置 agent：无 skill，仅用模型 + 指示 + 黑板做纯对话 / 决策
            from skill_engine.models import Skill, SkillMetadata
            skill = Skill(
                metadata=SkillMetadata(
                    name=f"moa-builtin-{agent.alias}",
                    description=f"MOA 内置协作 agent {agent.alias}（无挂载 skill）",
                ),
                body="你是一个协作 agent。根据用户给的任务与当前协作状态完成工作，"
                     "最终用自然语言输出你的成果。\n\n$ARGUMENTS",
                directory=self.working_root,
                supporting_files=[],
            )

        composed = (
            f"{agent.instruction}\n\n"
            f"## 本轮任务（指挥官在第 {round_no} 轮指派）\n{task}\n\n"
            f"## 原始总任务\n{query}\n\n"
            f"## 当前协作状态（其他 agent 的已有产出，供你参考 / 接续，不要重复）\n"
            f"{session.blackboard_summary()}\n"
        )
        arguments = {"$ARGUMENTS": composed, "$0": composed, **parse_named_params(composed)}
        mr = MatchResult(skill=skill, score=1.0, method="moa", arguments=arguments)

        runner = ToolDispatchRunner(
            executor=self.executor,
            assembler=self.assembler,
            approval_fn=self.approval_fn,
            human_io=self.human_io,
            turn_policy=None,           # MOA worker 不内部循环追问，跑完即返回
            working_root=self.working_root,
            plain_text=self.plain_text,
            verbose=self.verbose,
        )
        result = runner.run(
            mr, agent.llm, max_iterations=max_agent_iterations,
            snapshot=session.snapshot, file_tracker=session.file_tracker,
            session_mode=False,
        )
        out = result.get("output", "") or ""
        files = result.get("ctx", {}).get("files_created", []) or []
        return out, files

    # ---- worker 执行（带异常隔离） ----
    def _run_agent_safe(self, agent: MoaAgent, session: MoaSession, task: str,
                        query: str, round_no: int, max_agent_iterations: int) -> tuple[str, list[str]]:
        """同 `_run_agent`，但把 worker 运行期异常隔离在单个 agent 内。

        异常不会向上传播拖垮整轮 MOA：记录一条**稳定指纹**的错误产出（含 agent
        代号与异常类型），使反震荡闸能识别「连续失败」而非空转；控制权交还指挥官，
        由它决定重试、换人或 STOP。
        """
        try:
            logger.debug("worker %s 启动 (skill=%s, round=%d)", agent.alias, agent.skill_name or "内置", round_no)
            out, files = self._run_agent(agent, session, task, query, round_no, max_agent_iterations)
            logger.debug("worker %s 完成 (产出 %d 字, 文件 %d 个)", agent.alias, len(out), len(files))
            return out, files
        except Exception as e:  # noqa: BLE001 — 单点失败需隔离，不影响整体协作
            logger.exception("worker %s 执行异常", agent.alias)
            self._emit(f"[ERROR] worker {agent.alias} 执行异常: {e}")
            return f"[ERROR] worker {agent.alias} 执行失败: {type(e).__name__}", []

    # ---- 最终综合 ----
    def _final_synthesis(self, commander: MoaAgent, session: MoaSession,
                        query: str) -> str:
        """让指挥官基于黑板产出最终综合结论（单步，失败回退到拼接黑板）。"""
        prompt = (
            f"用户原始任务：{query}\n\n"
            f"以下是各 agent 协作完成后的完整黑板记录：\n\n"
            f"{session.blackboard_summary(max_each=4000)}\n\n"
            f"请综合以上所有成果，输出**最终交付结论**（不要重复过程，聚焦结果、"
            f"关键文件、以及仍需人工确认的点）。"
        )
        try:
            resp = commander.llm.invoke(prompt)
            if isinstance(resp, dict):
                return resp.get("content", "") or str(resp)
            if hasattr(resp, "content"):
                return resp.content if isinstance(resp.content, str) else str(resp.content)
            return str(resp)
        except Exception as e:
            # 回退：直接拼接黑板
            return "\n\n".join(
                f"### {e2['alias']}（{e2['skill']}）\n{e2['output']}"
                for e2 in session.blackboard
            ) + f"\n\n[注] 最终综合失败，已回退拼接：{e}"

    # ---- 主入口 ----
    def run(
        self,
        agents: list[MoaAgent],
        commander: MoaAgent,
        registry,
        query: str = "",
        max_rounds: int = 8,
        max_agent_iterations: int = 12,
        max_llm_calls: int = 60,
        max_consecutive_same_agent: int = 3,
        final_synthesis: bool = True,
    ) -> dict:
        """运行 MOA 协作回路。

        Args:
            agents: worker agent 列表（至少 1 个）。
            commander: 指挥官 agent。
            registry: Skill 注册表（加载 skill 用）。
            query: 原始任务描述。
            max_rounds: 指挥官决策轮数上限（闸 #1）。
            max_agent_iterations: 单个 worker 内层 tool_dispatch 迭代上限（闸 #2）。
            max_llm_calls: 全局 LLM 调用次数上限（闸 #3，CountingLLM 计数）。
            max_consecutive_same_agent: 同 agent 连续命中上限（闸 #4 触发条件）。
            final_synthesis: 结束后是否让指挥官综合最终结论。

        Returns:
            结果 dict（含 output / rounds / llm_calls / stopped_by / files_created 等）。
        """
        self._registry = registry

        # ── 前置校验 ──
        if not agents:
            return self._result("", 0, 0, "no_agents", [], query, [])
        # alias 唯一性
        aliases = [a.alias for a in agents] + [commander.alias]
        if len(set(aliases)) != len(aliases):
            return self._result("", 0, 0, "duplicate_alias", [], query, [])

        # ── 实例化各 agent 的模型客户端（延迟到运行期，便于提前报缺配置） ──
        # 若调用方已预注入 a.llm（测试 / 复用客户端场景），则跳过联网取模型，
        # 但仍统一包一层 CountingLLM 以计入全局成本上限。
        counter = {"calls": 0, "prompt": 0, "completion": 0, "total": 0}
        try:
            from skill_engine.config import get_llm_by_profile
            for a in agents + [commander]:
                if a.llm is None:
                    a.llm = CountingLLM(get_llm_by_profile(a.model_profile), counter)
                else:
                    a.llm = CountingLLM(a.llm, counter)
        except Exception as e:
            return self._result("", 0, 0, "model_config_error",
                                [f"{a.alias}: {e}" for a in agents + [commander]], query, [])

        session = MoaSession(self.working_root)
        stopped_by = "max_rounds"
        last_rationale = ""

        self._emit_header(f"MOA 协作启动 @ {session.working_root}")
        self._emit(
            f"指挥官={commander.alias}({commander.model_profile}) · "
            f"workers={', '.join(a.alias for a in agents)} · "
            f"上限 {max_rounds} 轮 / {max_llm_calls} 次 LLM 调用"
        )

        for round_no in range(1, max_rounds + 1):
            # 闸 #3：全局 LLM 调用上限（counter 由 CountingLLM 实时累计）。
            # 在当前轮尚未派出任何 agent 前就已达上限 → 记「已完成轮数」= round_no-1。
            if counter["calls"] >= max_llm_calls:
                session.round = round_no - 1
                stopped_by = "max_llm_calls"
                logger.debug("闸#3 触发: LLM 调用 %d ≥ %d → 停止", counter["calls"], max_llm_calls)
                break
            session.round = round_no

            # 1) 指挥官决策
            c_prompt = self._commander_prompt(commander, session, agents, query,
                                              round_no, max_rounds)
            try:
                c_resp = commander.llm.invoke(c_prompt)
            except Exception as e:
                stopped_by = "commander_error"
                self._emit(f"[ERROR] 指挥官调用失败: {e}")
                break
            c_text = (c_resp.get("content") if isinstance(c_resp, dict)
                      else getattr(c_resp, "content", str(c_resp)))
            if isinstance(c_text, (list, dict)):
                c_text = str(c_text)
            decision = self._parse_decision(c_text, agents)
            last_rationale = decision.get("rationale", "")

            self._emit(
                f"> 第 {round_no}/{max_rounds} 轮 · 指挥官决策: "
                f"next={decision['next']} · {decision.get('rationale','')[:80]}"
            )

            if decision["next"] == "STOP":
                stopped_by = "commander_stop"
                break

            agent = next((a for a in agents if a.alias == decision["next"]), None)
            if agent is None:
                stopped_by = "commander_invalid_agent"
                break

            # 闸 #4 计数：连续同 agent
            if agent.alias == session.last_agent_alias:
                session.same_agent_streak += 1
            else:
                session.same_agent_streak = 1
                session.last_agent_alias = agent.alias

            # 2) 执行 worker（异常隔离，单点失败不拖垮整轮）
            out, files = self._run_agent_safe(agent, session, decision["task"], query,
                                              round_no, max_agent_iterations)
            session.add_entry(agent.alias, agent.skill_name, out, files)
            self._emit(f"   └─ {agent.alias} 完成（产出 {len(out)} 字，文件 {len(files)} 个）")

            # 闸 #4 触发：连续同 agent 且黑板无进展（增量指纹不变）
            h = session.last_entry_hash()
            if h == session.prev_blackboard_hash:
                session.no_progress_streak += 1
            else:
                session.no_progress_streak = 0
                session.prev_blackboard_hash = h
            if (session.same_agent_streak >= max_consecutive_same_agent
                    and session.no_progress_streak >= 1):
                stopped_by = "anti_loop_forced_stop"
                logger.debug("闸#4 触发: %s 连续 %d 轮无进展 → 强制停止",
                             agent.alias, session.same_agent_streak)
                self._emit(
                    f"[防死循环] {agent.alias} 连续 {session.same_agent_streak} 轮且黑板无进展，强制停止"
                )
                break

        # 最终综合
        final_output = ""
        session.llm_calls = counter["calls"]   # 同步全局计数到报告字段
        if session.blackboard:
            if final_synthesis:
                final_output = self._final_synthesis(commander, session, query)
            else:
                final_output = "\n\n".join(
                    f"### {e['alias']}（{e['skill']}）\n{e['output']}"
                    for e in session.blackboard
                )
        else:
            final_output = "（无 agent 产出）"

        all_files = []
        for e in session.blackboard:
            all_files.extend(e.get("files") or [])

        return self._result(
            final_output, session.round, session.llm_calls, stopped_by,
            [a.summary() for a in agents + [commander]], query, all_files,
            last_rationale,
            tokens_prompt=counter["prompt"],
            tokens_completion=counter["completion"],
            tokens_total=counter["total"],
        )

    @staticmethod
    def _result(output, rounds, llm_calls, stopped_by, agent_summaries,
                query, files_created, last_rationale="",
                tokens_prompt=0, tokens_completion=0, tokens_total=0) -> dict:
        return {
            "skill_name": "moa",
            "output": output,
            "rounds": rounds,
            "llm_calls": llm_calls,
            "stopped_by": stopped_by,
            "agents": agent_summaries,
            "query": query,
            "files_created": files_created,
            "last_rationale": last_rationale,
            "tokens_prompt": tokens_prompt,
            "tokens_completion": tokens_completion,
            "tokens_total": tokens_total,
        }
