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
1. ``max_rounds``：commander 决策轮数硬上限（默认 20）。
2. ``max_agent_iterations``：单个 worker 内层 tool_dispatch 迭代上限（默认 60）。
3. ``max_llm_calls``：全局 LLM 调用次数上限（``CountingLLM`` 计数，默认 500）。
4. 反震荡强制停止：同一 agent 连续命中 ``max_consecutive_same_agent``(默认 3)
   次且黑板无变化 → 强制 STOP，避免 commander 在两个 agent 间无限乒乓。

 commander 决策解析失败时**默认 STOP**（fail-safe），绝不因解析异常而继续空转。
"""

import os
import re
import json
import hashlib
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

from skill_engine.models import MoaAgent, MatchResult

logger = logging.getLogger(__name__)
from skill_engine.execution.tool_dispatch import ToolDispatchRunner
from skill_engine.execution.tool_defs import parse_named_params
from skill_engine.execution.snapshot import FileSnapshot
from skill_engine.execution.file_tracker import FileStateTracker
from skill_engine.execution.paths import to_native_path
from skill_engine.routing.router import Router
from skill_engine import ensure_utf8_io


# ── 全局 LLM 调用计数包装（成本闸门 #3） ────────────────────────────────────
# 抽成共享模块，供 moa 与 run 命令（baseline run -td 埋 token）共用同一套机制。
from .counting_llm import CountingLLM, _accumulate_tokens
from .context_manager import ContextManager, default_context_budget


# ── 指挥官专用 skill 白名单（问题 3：只能从这里选，未来可扩展） ────────────
# worker 自动匹配 skill 时也会排除这些（指挥官 skill 不派给 worker）。
MOA_COMMANDER_SKILLS: tuple[str, ...] = ("moa-commander",)

# ── key_facts：黑板结构化要点（抗压缩） ─────────────────────────────────────
# worker 产出末尾约定写一段【关键事实】清单，_extract_key_facts 解析为结构化
# 列表，与 output 并列存储/传输。压缩与截断丢的是长文本冗余描述，关键事实
# 以最精炼形态独立流转——progress_summary / blackboard_summary 优先展示。
FACTS_MAX_ITEMS = 20          # 单条产出最多提取的事实条数
FACTS_MAX_LEN = 60            # 单条事实长度上限（超出截断，防 facts 变第二份长文本）
FACTS_PROMPT_HINT = (
    "\n\n## 产出格式约定（重要）\n"
    "你的最终产出末尾必须追加一段【关键事实】清单：每行一条（以 - 开头），"
    "每条是一句精炼、自包含、可独立引用的事实（尽量 ≤30 字），供指挥官跨轮"
    "引用与决策。示例：\n"
    "【关键事实】\n"
    "- 品牌图标与设计稿不一致（红色实心圆 vs 类似苹果logo）\n"
    "- 主色调已对齐设计稿 #EE6A4D\n"
    "若本轮没有值得记录的要点，写【关键事实】\n- 无"
)


# ── 共享黑板 / 会话状态 ────────────────────────────────────────────────────
@dataclass
class MoaAgentRuntime:
    """单个 agent（含 commander）的跨轮私有运行时上下文。

    与共享黑板正交：这里只装该 agent 自己的 messages（任务轮次 + 工具步骤 +
    自身答案 + 黑板精选摘要），绝不混入其他 agent 的私有历史。messages 跨轮持久，
    由 ``ToolDispatchRunner.run`` 内部的 ``ContextManager`` 负责压缩；每次跑完把
    ``result.history`` 回写此处即可实现"记得上次做了啥"。
    """
    alias: str
    messages: list[dict] = field(default_factory=list)
    last_output: str = ""
    cumulative_iterations: int = 0
    files: list[str] = field(default_factory=list)


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
        # 每 agent（含 commander）独立的跨轮私有上下文：隔离记忆，互不共享
        self.agent_contexts: dict[str, "MoaAgentRuntime"] = {}
        # 指挥官决策轨迹（供跨轮决策参考 + 最终综合）
        self.commander_decisions: list[dict] = []
        self.round = 0
        self.llm_calls = 0                  # 经 CountingLLM 累计
        # 防震荡
        self.last_agent_alias: Optional[str] = None
        self.same_agent_streak = 0
        self.prev_blackboard_hash: Optional[int] = None
        self.no_progress_streak = 0
        self.action_counts: dict[str, int] = {}   # alias → 已行动轮次（防早停闸用）

    def ctx(self, alias: str) -> "MoaAgentRuntime":
        """按 alias 懒创建/获取私有运行时上下文（含 commander）。"""
        return self.agent_contexts.setdefault(alias, MoaAgentRuntime(alias=alias))

    def add_entry(self, alias: str, skill: str, output: str, files: list[str],
                  status: str = "ok", iterations: int = 0, reason: str = "",
                  key_facts: Optional[list[str]] = None) -> None:
        """登记一笔 worker 产出。

        status: 终止状态（ok=正常完成；max_iterations / error 等=被打断、未完成）。
        iterations: 该 worker 本轮实际执行的工具迭代轮数。
        reason: 原始停止原因（stopped_by 原值）。
        key_facts: 结构化关键事实清单（从产出【关键事实】段解析），与 output
            并列存储——压缩/截断丢的是长文本冗余描述，facts 以精炼形态流转。
        """
        self.blackboard.append({
            "alias": alias,
            "skill": skill or "（内置）",
            "output": output or "",
            "files": list(files or []),
            "status": status,
            "iterations": int(iterations or 0),
            "reason": reason,
            "key_facts": list(key_facts or []),
        })
        self.action_counts[alias] = self.action_counts.get(alias, 0) + 1

    def last_entry_hash(self) -> int:
        """最近一笔产出的指纹，用于「无进展」检测。

        只取**最新一笔**（增量），而非整块黑板——否则每轮新增一条 entry 都会
        让整体 hash 变化，导致「连续同 agent 产出相同」永远判不出「无进展」。
        指纹含 alias + 输出正文 + 首个文件，比仅用长度更抗碰撞。

        使用 hashlib 计算**确定性**指纹（而非内置 ``hash()``）：内置 ``hash()``
        逐进程加盐，状态文件跨进程（崩溃续跑）重载后无法与现场重新计算的指纹
        对齐，会让反震荡闸在续跑后失效。确定性指纹保证落盘/重载一致。
        """
        if not self.blackboard:
            return 0
        e = self.blackboard[-1]
        payload = f"{e['alias']}|{e['output']}|{(e['files'] or [''])[0]}"
        return int.from_bytes(hashlib.md5(payload.encode("utf-8")).digest()[:8], "big")

    def _facts_line(self, e: dict, max_items: int = 3) -> str:
        """黑板条目的关键事实行（无 facts → 空串，兼容旧状态文件）。"""
        facts = e.get("key_facts") or []
        if not facts:
            return ""
        return "[关键事实] " + "；".join(facts[:max_items])

    def blackboard_summary(self, max_each: int = 5000) -> str:
        """生成供注入 prompt 的共享状态摘要（截断，控制上下文体积）。

        保留全部条目（供最终综合等一次性全量场景）；每条先给结构化关键事实
        行（抗截断的要点），再给 output 正文。
        """
        if not self.blackboard:
            return "（尚无任何 agent 产出）"
        lines = []
        for i, e in enumerate(self.blackboard, 1):
            out = e["output"] or "（无文本产出）"
            if len(out) > max_each:
                out = out[:max_each] + f" …(截断，共 {len(e['output'])} 字)"
            files = ("；文件: " + ", ".join(e["files"])) if e.get("files") else ""
            status_tag = _status_tag(e)
            head = f"### 第 {i} 步 · {e['alias']}（skill={e['skill']}）{status_tag}"
            fl = self._facts_line(e)
            lines.append(f"{head}\n{fl}\n{out}{files}" if fl else f"{head}\n{out}{files}")
        return "\n\n".join(lines)

    def progress_summary(self, recent_ok: int = 3, ok_max: int = 4000,
                         old_ok_max: int = 120) -> str:
        """指挥官决策视图：异常全量 + 新近全量 + 旧条目一行（关键事实优先）。

        - 未完成 / 异常 worker：完整条目 + 终止原因/轮数（commander 必须看清）；
        - 正常产出：最近 recent_ok 条给全量（关键信息还在热区，避免信息峰值
          在正文后部时被概览截掉），更早的折叠为一行式概览——概览行优先展示
          key_facts（结构化要点，截断不心疼），无 facts 时回退正文前若干字。
        """
        if not self.blackboard:
            return "（尚无任何 agent 产出）"
        aborted = [e for e in self.blackboard if e.get("status") and e["status"] != "ok"]
        ok_entries = [e for e in self.blackboard
                      if not e.get("status") or e["status"] == "ok"]
        parts = []
        if aborted:
            lines = []
            for e in aborted:
                out = e["output"] or "（无文本产出）"
                if len(out) > 4000:
                    out = out[:4000] + f" …(截断，共 {len(e['output'])} 字)"
                fl = self._facts_line(e)
                head = f"### {e['alias']}（skill={e['skill']}）{_status_tag(e)}"
                lines.append(f"{head}\n{fl}\n{out}" if fl else f"{head}\n{out}")
            parts.append("# ⚠ 未完成 / 异常 worker（其任务大概率未完成，必须优先处理）\n"
                         + "\n\n".join(lines))
        if ok_entries:
            recent = ok_entries[-recent_ok:]
            older = ok_entries[:-recent_ok] if len(ok_entries) > recent_ok else []
            if older:
                lines = []
                for e in older:
                    facts = e.get("key_facts") or []
                    files = (" · 文件: " + ", ".join(e["files"][:5])) if e.get("files") else ""
                    if facts:
                        lines.append(f"- {e['alias']}（skill={e['skill']}）{files}"
                                     f" · [要点] {'；'.join(facts[:2])}"
                                     f" · {(e['output'] or '')[:old_ok_max]}")
                    else:
                        out = (e["output"] or "").replace("\n", " ")[:old_ok_max] or "（无文本产出）"
                        lines.append(f"- {e['alias']}（skill={e['skill']}）{files} · {out}")
                parts.append("# 更早产出（一行概览 · 关键事实优先）\n" + "\n".join(lines))
            lines = []
            for e in recent:
                out = e["output"] or "（无文本产出）"
                if len(out) > ok_max:
                    out = out[:ok_max] + f" …(截断，共 {len(e['output'])} 字)"
                files = ("；文件: " + ", ".join(e["files"][:5])) if e.get("files") else ""
                fl = self._facts_line(e)
                head = f"### {e['alias']}（skill={e['skill']}）"
                lines.append(f"{head}\n{fl}\n{out}{files}" if fl else f"{head}\n{out}{files}")
            parts.append(f"# 最近产出（最近 {len(recent)} 条 · 全量）\n"
                         + "\n\n".join(lines))
        return "\n\n".join(parts)


_OK_STOPS = {"stop", "tool_stop", "session_turn_end", "max_turns", "user_exit"}

# 正常终止原因（与崩溃/异常终止相对）：这些情况下协作已"干净结束"，运行结束
# 后删除自动检查点，避免陈旧的状态文件误导后续 --resume-from。其余终止原因
# （commander_error / commander_invalid_agent / model_config_error 等）视为
# 非正常结束，保留检查点以便用户 --resume-from 续跑。
_CLEAN_STOP_REASONS = {
    "commander_stop",
    "max_rounds",
    "max_llm_calls",
    "anti_loop_forced_stop",
}

_STATUS_TEXT = {
    "max_iterations": "达到最大迭代次数",
    "error": "异常终止",
    "rate_limited": "被限流终止",
    "commander_stop": "正常停止",
    "anti_loop_forced_stop": "防死循环强制停止",
}


def _status_tag(e: dict) -> str:
    """黑板条目的状态标注（异常条目加 ⚠）。"""
    st = e.get("status") or "ok"
    if st == "ok":
        return ""
    txt = _STATUS_TEXT.get(st, st)
    it = e.get("iterations") or 0
    return f"[⚠ {txt} · 已执行 {it} 轮 · 任务可能未完成]"


def _try_remove(path: str) -> None:
    """尽力删除检查点文件（失败静默）。"""
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _extract_key_facts(output: str) -> list[str]:
    """从 worker 产出中提取【关键事实】清单（容错：无标记 → []）。

    逐行解析：标记行之后以 - / * / • / 数字开头的行都是事实条目；空行或
    普通正文行终止提取。每条钳制长度与条数，防止格式漂移把 facts 变成
    第二份长文本。解析失败一律降级为空列表，不影响主流程。
    """
    if not output:
        return []
    facts: list[str] = []
    started = False
    for line in output.splitlines():
        s = line.strip()
        if not started:
            if re.search(r"【\s*关键事实\s*】", s):
                started = True
            continue
        if not s:
            break
        if re.match(r"^[-*•·]\s*", s):
            item = re.sub(r"^[-*•·]\s*", "", s)
        elif re.match(r"^\d+[.、)]\s*", s):
            item = re.sub(r"^\d+[.、)]\s*", "", s)
        elif not facts:
            item = s          # 容错：标记后首行无符号也当事实
        else:
            break             # 普通正文行 → 段结束
        item = item.strip()
        if not item or item in ("无", "无。", "None", "N/A"):
            continue
        if len(item) > FACTS_MAX_LEN:
            item = item[:FACTS_MAX_LEN] + "…"
        facts.append(item)
        if len(facts) >= FACTS_MAX_ITEMS:
            break
    return facts


def _classify_stop(stopped_by: str, iterations: int) -> tuple[str, int, str]:
    """把 tool_dispatch 的停止原因映射为黑板状态。

    Returns: (status, iterations, reason) —— status ∈ ok / 原始停止原因。
    """
    sb = stopped_by or ""
    if sb in _OK_STOPS:
        return "ok", int(iterations or 0), sb
    if not sb:
        return "ok", int(iterations or 0), ""
    return sb, int(iterations or 0), sb


def _eager_fold_old_rounds(messages: list[dict]) -> None:
    """轮末 eager L1：原地折叠 keep_recent 之外的旧轮超大 tool 输出与旧轮思考。

    免费、确定性、零 LLM 调用——与 run 循环内的 lazy L1 共用同一套
    ``ContextManager._micro_compact`` 逻辑。保证私有上下文跨轮处于"干净态"：
    状态落盘精简，且下次派发时无需再折同样的内容（幂等）。
    """
    if not messages:
        return
    cm = ContextManager(budget=default_context_budget(), compact_tool_output=True)
    cm.messages = messages
    cm._micro_compact()


def _merge_files(existing: list[str], new_files: list[str]) -> None:
    """把本轮产出文件去重合并进累计文件列表（文档 §3.1：files 为累计产出文件）。"""
    for f in new_files:
        if f not in existing:
            existing.append(f)


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
        trusted_root: Optional[str] = None,
    ):
        self.executor = executor
        self.assembler = assembler
        self.approval_fn = approval_fn
        self.human_io = human_io
        self.working_root = str(to_native_path(working_root) or Path.cwd())
        self.trusted_root = trusted_root
        self.plain_text = plain_text
        self.verbose = verbose
        self._router = None                     # 懒构建（worker 自动匹配 skill 用）
        self._auto_skill_cache: dict[str, str] = {}  # alias → 自动匹配到的 skill 名
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
        ensure_utf8_io()  # 重定向到文件时防止 gbk print 崩（↳ 等符号）
        m = getattr(self.human_io, "emit", None)
        if callable(m):
            try:
                m(text, label="[MOA] ")
            except TypeError:  # 旧式 human_io（无 label 参数）兼容
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
        counts = session.action_counts
        try:
            from skill_engine.config import list_model_profiles
            _profiles = list_model_profiles()
        except Exception:  # noqa: BLE001 — 能力标注失败不影响决策
            _profiles = {}
        roster = "\n".join(
            f"- {a.alias}（{'指挥官' if a.role=='commander' else 'worker'}）："
            f"模型={a.model_profile}（{'视觉✓' if _profiles.get(a.model_profile, {}).get('vision') else '文本'}），"
            f"skill={a.skill_name or '内置'}，"
            f"已行动 {counts.get(a.alias, 0)} 次，"
            f"职责={a.instruction or '（未指定）'}"
            for a in agents
        )
        # 尚未行动的 worker 及其职责（程序显式列出，杜绝"模型从黑板倒推漏人"）
        idle = [a for a in agents if counts.get(a.alias, 0) == 0
                and (a.instruction or "").strip()]
        idle_lines = "\n".join(
            f"- {a.alias}（{a.instruction.strip().replace(chr(10), ' ')[:120]}）"
            for a in idle
        ) if idle else "（全部 worker 均已至少行动过一次）"
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
        # 指挥官自身决策轨迹（跨轮累积于私有上下文；此处再给一份结构化摘要，
        # 抗压缩、便于跨轮避免重复派活 / 乒乓）
        decision_history = ""
        if session.commander_decisions:
            recent = session.commander_decisions[-6:]
            lines = "\n".join(
                f"- 第 {d['round']} 轮：派 {d['next']}"
                f"（{(d.get('task') or '')[:80]}）"
                for d in recent
            )
            decision_history = f"\n# 你之前的决策轨迹（避免重复派活 / 乒乓）\n{lines}\n"
        return f"""\
你是一个多智能体协作任务的**指挥官（commander）**。你不直接写代码或改文件，
你的唯一职责是：根据当前进展，决定**下一轮由哪个 worker agent 行动、干什么**。

# 原始任务
{query}

# 可用 worker agent 名册
{roster}

# 尚未行动的 worker（一次都还没被派发过——任务没做完的最强信号）
{idle_lines}

# 当前协作进度（未完成/异常 worker 全量 + 正常产出概览）
{session.progress_summary()}
{decision_history}
# 决策要求
- 你正处在第 {round_no}/{max_rounds} 轮。
- 只能在以下代号中选一个作为 next：{aliases}，或输出 STOP 表示任务已完成。
- 若选某个 worker，必须在 task 中写清**本轮具体要它做什么**（结合黑板现状，
  避免重复已经做过的事）。
- **硬约束：若存在「尚未行动」的 worker（上面清单列出），禁止输出 STOP**——
  必须指派它执行其职责范围内的首轮工作，哪怕你觉得其他 worker 已经做完了。
  它们的职责是任务的一部分，尚未执行 = 任务未完成。
- **未完成 worker 优先续派**：若「未完成 / 异常」段列出某 worker（如达到最大
  迭代次数、异常终止），其职责大概率未完成 → **优先再次指派它**，并在 task 中
  明确指示它**基于自己最后一次产出继续工作**（不要从头重做）。若同 worker
  连续多轮续派仍无进展，再考虑换人。
- **按能力派活**：名册中标注「文本」的 worker 不能看图片、不能截图——涉及
  视觉检查/UI 渲染验证的任务必须指派「视觉✓」的 worker；「文本」worker 改用
  读源码、跑测试等方式验证。
- **worker 的产出只是自评**：它会宣称"完成 / 全部通过"，这不等于任务达成。
  若任务涉及它职责未覆盖的环节（如检查、审查、验证、测试），必须指派对应
  worker 实际执行后才能 STOP。
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
                   query: str, round_no: int, max_agent_iterations: int) -> tuple[str, list[str], dict]:
        """执行单个 worker：组装 (model × skill × 指令+黑板) 并跑一次 tool_dispatch。

        Returns: (output, files, status) —— status = {"code", "iterations", "reason"}，
        其中 code 为 "ok" 或 tool_dispatch 的 stopped_by 原值（max_iterations/error 等
        → 该 worker 被打断、任务未完成，指挥官应据此续派恢复）。
        """
        if agent.skill_name:
            skill = self._registry.load_skill(agent.skill_name)
            if not skill:
                return ("[ERROR] 无法加载 worker skill: " + agent.skill_name, [],
                        {"code": "error", "iterations": 0,
                         "reason": f"skill 加载失败: {agent.skill_name}"})
        else:
            # worker 未配置 skill → 按指示自动匹配现有 skill（问题 4），
            # 匹配不到（或命中指挥官专用 skill）才退回内置纯模型。
            skill = self._auto_match_skill(agent)
            if skill is not None:
                agent.skill_name = skill.metadata.name
            else:
                self._emit(f"[{agent.alias}] 未配置 skill 且未命中现有 skill → 内置（纯模型）")
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

        # —— 私有上下文：首轮完整 composed；续跑仅注入新轮 task + 完整黑板 ——
        # 隔离保证：actx.messages 只含该 agent 自己的轮次与黑板精选摘要，
        # 绝不混入其他 agent 的私有历史。
        actx = session.ctx(agent.alias)
        if actx.messages:
            # 续跑：该 agent 已有跨轮私有历史，新轮作为一条 user 消息追加，
            # 整体作为 initial_messages 交给 run()，由其复用自身记忆。
            composed = (
                f"## 指挥官在第 {round_no} 轮指派给你的新任务\n{task}\n\n"
                f"## 需要你知晓的协作上下文（其他 agent 的产出，非其私有历史）\n"
                f"{session.blackboard_summary()}\n"
                + FACTS_PROMPT_HINT
            )
            initial_messages = actx.messages
        else:
            # 首轮：照旧完整 composed（含 instruction / 原始总任务 / 黑板 / env 头）
            composed = (
                f"{agent.instruction}\n\n"
                f"## 本轮任务（指挥官在第 {round_no} 轮指派）\n{task}\n\n"
                f"## 原始总任务\n{query}\n\n"
                f"## 当前协作状态（其他 agent 的已有产出，供你参考 / 接续，不要重复）\n"
                f"{session.blackboard_summary()}\n"
                + FACTS_PROMPT_HINT
            )
            initial_messages = None

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
            trusted_root=self.trusted_root,
        )
        result = runner.run(
            mr, agent.llm, max_iterations=max_agent_iterations,
            snapshot=session.snapshot, file_tracker=session.file_tracker,
            session_mode=False,
            initial_messages=initial_messages,
            append_final_prompt=bool(actx.messages),
        )
        # —— 回写私有上下文（result.history 已被 run 内部 ContextManager 压缩过）——
        # 下次再派该 agent 时，它即可从这份历史里"记得上次做了啥"。
        actx.messages = list(result.history)
        # 轮末 eager L1：立即折掉超出 keep_recent 的旧轮大 tool 输出与旧轮思考，
        # 私有上下文保持"干净态"（落盘精简 / 下次续跑无需重复折叠）。
        _eager_fold_old_rounds(actx.messages)
        actx.last_output = result.get("output", "") or ""
        actx.cumulative_iterations += int(result.get("iterations", 0) or 0)
        round_files = result.get("files_created", []) or []
        # 累计产出文件（跨轮去重合并进 actx.files；黑板只记本轮文件）
        _merge_files(actx.files, round_files)

        out = actx.last_output
        # 注意：不能用 result.get("ctx", {})——RunResult.get 对 "ctx" 键无特判且 ctx
        # 内不含 "ctx" 键，会返回默认 {}；files_created 是 ctx 内的键，get 会透传。
        files = round_files
        sb = result.get("stopped_by", "") or ""
        status_code, status_iter, _ = _classify_stop(sb, int(result.get("iterations", 0) or 0))
        status = {"code": status_code, "iterations": status_iter, "reason": sb}
        return out, files, status

    def _auto_match_skill(self, agent: MoaAgent):
        """worker 未配置 skill 时按指示自动匹配现有 skill（复用 Router.match）。

        结果按 alias 缓存（避免每轮重复匹配 / 重复 LLM 调用）。命中指挥官专用
        skill 或匹配失败时返回 None → 调用方回退内置纯模型。
        """
        if agent.alias in self._auto_skill_cache:
            name = self._auto_skill_cache[agent.alias]
            return self._registry.load_skill(name) if name else None
        try:
            if self._router is None:
                self._router = Router(self._registry, verbose=False)
            plan = self._router.match(agent.instruction or "")
            name = plan.primary.name if plan and plan.primary else ""
            if name and name not in MOA_COMMANDER_SKILLS:
                skill = self._registry.load_skill(name)
                if skill is not None:
                    self._auto_skill_cache[agent.alias] = name
                    self._emit(
                        f"[{agent.alias}] 未配置 skill → 已按描述自动匹配: {name}"
                        f"（score={plan.score:.2f}，方法={plan.method}）"
                    )
                    return skill
        except Exception as e:  # noqa: BLE001 — 匹配失败不拖垮 MOA，回退内置
            logger.debug("worker %s 自动匹配 skill 失败: %s", agent.alias, e)
        self._auto_skill_cache[agent.alias] = ""
        return None

    # ---- worker 执行（带异常隔离） ----
    def _run_agent_safe(self, agent: MoaAgent, session: MoaSession, task: str,
                        query: str, round_no: int, max_agent_iterations: int) -> tuple[str, list[str], dict]:
        """同 `_run_agent`，但把 worker 运行期异常隔离在单个 agent 内。

        异常不会向上传播拖垮整轮 MOA：记录一条**稳定指纹**的错误产出（含 agent
        代号与异常类型），使反震荡闸能识别「连续失败」而非空转；控制权交还指挥官，
        由它决定重试、换人或 STOP。异常同样以 status={"code": "error"} 标记，
        供指挥官识别「未完成、需恢复」的 worker。
        """
        try:
            logger.debug("worker %s 启动 (skill=%s, round=%d)", agent.alias, agent.skill_name or "内置", round_no)
            out, files, status = self._run_agent(agent, session, task, query, round_no, max_agent_iterations)
            return out, files, status
        except Exception as e:  # noqa: BLE001 — 单点失败需隔离，不影响整体协作
            logger.exception("worker %s 执行异常", agent.alias)
            self._emit(f"[ERROR] worker {agent.alias} 执行异常: {e}")
            return (f"[ERROR] worker {agent.alias} 执行失败: {type(e).__name__}", [],
                    {"code": "error", "iterations": 0, "reason": f"{type(e).__name__}: {e}"})

    # ---- 最终综合 ----
    def _final_synthesis(self, commander: MoaAgent, session: MoaSession,
                        query: str) -> str:
        """让指挥官基于黑板产出最终综合结论（单步，失败回退到拼接黑板）。"""
        seq = "；".join(
            f"R{d['round']}→{d['next']}" for d in session.commander_decisions
        ) or "（无）"
        prompt = (
            f"用户原始任务：{query}\n\n"
            f"以下是各 agent 协作完成后的完整黑板记录：\n\n"
            f"{session.blackboard_summary(max_each=4000)}\n\n"
            f"本次协作指挥官逐轮指派序列：{seq}\n\n"
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
        max_rounds: int = 20,
        max_agent_iterations: int = 60,
        max_llm_calls: int = 500,
        max_consecutive_same_agent: int = 3,
        final_synthesis: bool = True,
        resume_from: Optional[str] = None,
        state_path: Optional[str] = None,
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
            resume_from: 可选，从指定状态文件续跑（载入各 agent 私有上下文 / 黑板 /
                决策轨迹 / 计数器，从断点继续整轮协作）。文件不存在/损坏 →
                返回 resume_state_missing 终止。
            state_path: 可选，运行状态落盘路径（每轮完成后写检查点，崩溃最多丢
                本轮在途工作）。省略时默认落到工作目录 moa_session_state.json；
                正常完成后自动删除，避免陈旧文件误导后续 --resume-from。

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

        # ── 续跑恢复（Phase 3）──
        # 从断点状态文件载入 session（各 agent 私有上下文 / 黑板 / 决策轨迹 /
        # 计数器），继续整轮协作；否则全新启动。
        if resume_from:
            restored = self._load_moa_state(resume_from)
            if restored is None:
                return self._result("", 0, 0, "resume_state_missing", [], query, [])
            session = restored["session"]
            counter = restored["counter"]
            # 续跑沿用同一状态文件路径（若未显式另指），边跑边更新检查点
            state_path = state_path or resume_from
        else:
            session = MoaSession(self.working_root)
            counter = {"calls": 0, "prompt": 0, "completion": 0, "total": 0}

        # ── 自动检查点 ──
        # 未显式指定 state_path 时，默认落到工作目录 moa_session_state.json：
        # 每轮正常完成后落盘，崩溃最多丢失本轮在途工作；正常完成后删除，避免
        # 陈旧文件误导后续 --resume-from（续跑命令见 cli 提示）。
        if state_path is None:
            state_path = os.path.join(self.working_root, "moa_session_state.json")

        # ── 实例化各 agent 的模型客户端（延迟到运行期，便于提前报缺配置） ──
        # 若调用方已预注入 a.llm（测试 / 复用客户端场景），则跳过联网取模型，
        # 但仍统一包一层 CountingLLM 以计入全局成本上限（续跑时复用恢复出的 counter）。
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

        stopped_by = "max_rounds"
        last_rationale = ""

        self._emit_header(f"MOA 协作启动 @ {session.working_root}")
        self._emit(
            f"指挥官={commander.alias}({commander.model_profile}) · "
            f"workers={', '.join(a.alias for a in agents)} · "
            f"上限 {max_rounds} 轮 / {max_llm_calls} 次 LLM 调用"
        )

        # 续跑从「已完成轮数 + 1」继续；max_rounds 作为绝对预算（总轮数上限），
        # 即续跑沿用原 max_rounds，剩余可用轮数 = max_rounds - session.round。
        start_round = session.round + 1 if resume_from else 1
        for round_no in range(start_round, max_rounds + 1):
            # 闸 #3：全局 LLM 调用上限（counter 由 CountingLLM 实时累计）。
            # 在当前轮尚未派出任何 agent 前就已达上限 → 记「已完成轮数」= round_no-1。
            if counter["calls"] >= max_llm_calls:
                session.round = round_no - 1
                stopped_by = "max_llm_calls"
                logger.debug("闸#3 触发: LLM 调用 %d ≥ %d → 停止", counter["calls"], max_llm_calls)
                break
            session.round = round_no

            # 1) 指挥官决策（指挥官拥有独立隔离上下文，跨轮累积决策轨迹）
            c_prompt = self._commander_prompt(commander, session, agents, query,
                                              round_no, max_rounds)
            try:
                c_ctx = session.ctx(commander.alias)
                c_ctx.messages.append({"role": "user", "content": c_prompt})
                c_cm = ContextManager(budget=default_context_budget())
                c_cm.messages = c_ctx.messages
                c_cm.maybe_compress(commander.llm)   # 复用 L1/L2/L3（指挥官无工具，仅压缩）
                c_resp = commander.llm.invoke(c_cm.messages)
                c_text = (c_resp.get("content") if isinstance(c_resp, dict)
                          else getattr(c_resp, "content", str(c_resp)))
                if isinstance(c_text, (list, dict)):
                    c_text = str(c_text)
                c_ctx.messages.append({"role": "assistant", "content": c_text})
                c_ctx.last_output = c_text
            except Exception as e:
                stopped_by = "commander_error"
                self._emit(f"[ERROR] 指挥官调用失败: {e}")
                break
            decision = self._parse_decision(c_text, agents)
            last_rationale = decision.get("rationale", "")
            # 决策理由回写（供跨轮决策参考 + 最终综合）
            session.commander_decisions.append({
                "round": round_no, "next": decision["next"],
                "task": decision.get("task", ""), "rationale": decision.get("rationale", ""),
            })

            self._emit(
                f"> 第 {round_no}/{max_rounds} 轮 · 指挥官决策: "
                f"next={decision['next']} · {decision.get('rationale','')}"
            )

            if decision["next"] == "STOP":
                # 防早停硬闸：存在从未行动且已声明职责的 worker → 禁止 STOP，改派。
                idle = [a for a in agents if session.action_counts.get(a.alias, 0) == 0
                        and (a.instruction or "").strip()]
                if idle:
                    agent = idle[0]
                    decision = {"next": agent.alias,
                                "task": "执行你的职责范围内的首轮工作（结合原始总任务与黑板）",
                                "rationale": f"防早停：{agent.alias} 尚未行动，禁止 STOP"}
                    self._emit(
                        f"[防早停] {agent.alias} 尚未行动（职责已声明），指挥官 STOP 被拦截，改派首轮工作"
                    )
                else:
                    stopped_by = "commander_stop"
                    break

            agent = next((a for a in agents if a.alias == decision["next"]), None)
            if agent is None:
                stopped_by = "commander_invalid_agent"
                break

            # agent 切换横幅：首次派活或换人时亮明身份（同 agent 连续多轮不重复）
            if session.last_agent_alias != agent.alias:
                self._emit_header(
                    f"[{agent.alias} · {agent.skill_name or '内置'} · {agent.model_profile}] "
                    f"开始本轮工作"
                )

            # 闸 #4 计数：连续同 agent
            if agent.alias == session.last_agent_alias:
                session.same_agent_streak += 1
            else:
                session.same_agent_streak = 1
                session.last_agent_alias = agent.alias

            # 2) 执行 worker（异常隔离，单点失败不拖垮整轮）
            out, files, status = self._run_agent_safe(agent, session, decision["task"], query,
                                                      round_no, max_agent_iterations)
            facts = _extract_key_facts(out)
            session.add_entry(agent.alias, agent.skill_name, out, files,
                              status=status["code"], iterations=status["iterations"],
                              reason=status["reason"], key_facts=facts)
            summary = out.strip().replace("\n", " ")[:800]
            aborted = status["code"] != "ok"
            verb = "中止" if aborted else "完成"
            line = f"   └─ {agent.alias} {verb}（产出 {len(out)} 字，文件 {len(files)} 个"
            if aborted:
                txt = _STATUS_TEXT.get(status["code"], status["code"])
                line += f"；{txt}·{status['iterations']} 轮"
            line += "）"
            if summary:
                line += f"\n       ↳ {summary}"
            if not aborted and out.strip() and not facts:
                line += "\n       ⚠ 产出未附【关键事实】要点（key_facts 为空，概览/压缩兜底未生效）"
            for f in files[:5]:
                line += f"\n       · {f}"
            self._emit(line)

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
                # 落盘当前轮（含触发的闸），便于崩溃续跑从断点继续
                if state_path:
                    self._save_moa_state(state_path, session, counter)
                break
            # ── 轮末检查点：每轮正常完成后落盘（崩溃最多丢失本轮在途工作）──
            if state_path:
                self._save_moa_state(state_path, session, counter)

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

        # ── 正常完成：清理检查点 ──
        # 仅 clean 终止原因才删（协作已干净结束）；崩溃 / 异常终止（commander_error
        # 等）保留检查点，便于用户 --resume-from 从断点继续。
        if state_path and stopped_by in _CLEAN_STOP_REASONS:
            _try_remove(state_path)

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

    # ── Phase 3：MOA 级状态持久化（崩溃续跑） ──────────────────────────────
    def _save_moa_state(self, path: str, session: "MoaSession", counter: dict) -> None:
        """将整轮 MOA 协作状态落盘为 JSON，供后续 --resume-from 续跑。

        持久化内容（均为 JSON 可序列化）：各 agent 私有上下文（messages /
        last_output / 累计迭代 / 产出文件）、黑板、指挥官决策轨迹、轮次与防死循环
        计数器、以及 LLM 成本计数器（续跑预算连续计数）。

        注意：snapshot / file_tracker 是运行时对象（文件已在 working_root 落盘），
        不序列化；续跑时 MoaSession.__init__ 重建即可，磁盘文件天然可见。
        失败静默，绝不拖垮主执行流程。
        """
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "version": 1,
                "round": session.round,
                "llm_calls": session.llm_calls,
                "last_agent_alias": session.last_agent_alias,
                "same_agent_streak": session.same_agent_streak,
                "prev_blackboard_hash": session.prev_blackboard_hash,
                "no_progress_streak": session.no_progress_streak,
                "action_counts": session.action_counts,
                "blackboard": session.blackboard,
                "commander_decisions": session.commander_decisions,
                "agent_contexts": {
                    alias: {
                        "alias": rt.alias,
                        "messages": rt.messages,
                        "last_output": rt.last_output,
                        "cumulative_iterations": rt.cumulative_iterations,
                        "files": rt.files,
                    }
                    for alias, rt in session.agent_contexts.items()
                },
                "counter": {
                    "calls": counter.get("calls", 0),
                    "prompt": counter.get("prompt", 0),
                    "completion": counter.get("completion", 0),
                    "total": counter.get("total", 0),
                },
            }
            p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception:
            # 状态持久化失败绝不影响主执行流程
            pass

    def _load_moa_state(self, path: str):
        """读取 MOA 状态文件；不存在 / 损坏 → 返回 None。

        Returns: {"session": MoaSession, "counter": dict} 或 None。
        重建的 session 复用当前 working_root（磁盘文件天然可见），prev_blackboard_hash
        因 last_entry_hash 已确定性化，直接载入即可与现场重新计算的指纹对齐。
        """
        try:
            p = Path(path)
            if not p.exists():
                return None
            state = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

        session = MoaSession(self.working_root)
        session.round = state.get("round", 0)
        session.llm_calls = state.get("llm_calls", 0)
        session.last_agent_alias = state.get("last_agent_alias")
        session.same_agent_streak = state.get("same_agent_streak", 0)
        session.prev_blackboard_hash = state.get("prev_blackboard_hash")
        session.no_progress_streak = state.get("no_progress_streak", 0)
        session.action_counts = state.get("action_counts", {}) or {}
        session.blackboard = state.get("blackboard", []) or []
        session.commander_decisions = state.get("commander_decisions", []) or []
        agent_contexts = {}
        for alias, rt in (state.get("agent_contexts", {}) or {}).items():
            agent_contexts[alias] = MoaAgentRuntime(
                alias=rt.get("alias", alias),
                messages=rt.get("messages", []) or [],
                last_output=rt.get("last_output", ""),
                cumulative_iterations=rt.get("cumulative_iterations", 0),
                files=rt.get("files", []) or [],
            )
        session.agent_contexts = agent_contexts
        counter = state.get("counter", {}) or {}
        counter = {
            "calls": counter.get("calls", 0),
            "prompt": counter.get("prompt", 0),
            "completion": counter.get("completion", 0),
            "total": counter.get("total", 0),
        }
        return {"session": session, "counter": counter}
