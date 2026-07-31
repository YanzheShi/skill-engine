"""
Runner — Skill 执行器，三路分流

职责：
1. 接收匹配结果
2. 三路分流：
   - 有 steps → _run_steps（确定性执行，Phase 3）
   - 有 llm → _run_llm_once（单次 LLM 调用，Phase 2 尾巴）
   - 都没有 → 只编译不执行（纯编译器模式）

使用方式：
>>> runner = Runner(assembler, executor)
>>> result = runner.run(match_result, llm=my_llm_client)
>>> print(result["output"])
"""

import os
from pathlib import Path
from typing import Optional
from skill_engine.models import Skill, MatchResult, Step
from skill_engine.execution.assembler import Assembler
from skill_engine.security.scanner import should_approve, _is_approved, _is_blocked, _save_approval, _save_blocklist
from skill_engine.execution.executor import Executor
from skill_engine.execution import tool_dispatch
from skill_engine.execution import steps as steps_runner
from skill_engine.execution.tool_defs import parse_named_params
from skill_engine.execution.paths import to_native_path, native_path_hint


class SkillSession:
    """单次 session 的持续状态持有者（由外层 REPL 维护）。

    薄数据对象：持有跨轮累积的对话历史 messages，以及落盘路径 state_path。
    不含执行逻辑（执行统一在 ToolDispatchRunner.run）。

    - messages: 跨轮累积的完整对话历史（含 system/user/assistant/tool）
    - state_path: 落盘路径；缺省落到 <working_root>/.workbuddy/sessions/<skill>.session.json
    - snapshot: 会话级文件检查点。整个 session 共用一个 FileSnapshot 实例，
      使 restore_file 能回滚到「会话起点」而非「本轮起点」。
    """

    def __init__(
        self,
        skill_name: str,
        working_root: Optional[str] = None,
        state_path: Optional[str] = None,
        messages: Optional[list] = None,
        snapshot=None,
    ):
        self.skill_name = skill_name
        self.working_root = working_root
        if state_path:
            self.state_path = state_path
        else:
            base = Path(working_root) if working_root else Path.cwd()
            self.state_path = str(base / ".workbuddy" / "sessions" / f"{skill_name}.session.json")
        self.messages: list = list(messages or [])
        if snapshot is None:
            from skill_engine.execution.snapshot import FileSnapshot
            snapshot = FileSnapshot(Path(working_root) if working_root else Path.cwd())
        self.snapshot = snapshot

    def append_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})


class Runner:
    """Skill 执行器 — 三路分流

    三种运行模式：
    1. 档位 A：单次 LLM 调用 — CC 原生 skill 零改造兼容
    2. Steps DSL：确定性执行 — engine 原生增强
    3. 纯编译：只返回 final prompt — pipe 给外部工具
    """

    def __init__(
        self,
        assembler: Assembler,
        executor: Executor,
        llm_api_base: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        llm_model: str = "",

    ):
        self.assembler = assembler
        self.executor = executor
        self.llm_api_base = llm_api_base
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model
        self._session_approvals: dict[str, bool] = {}  # op_str → True(允许) / False(拒绝)
        self._session_allow_all: bool = False  # 全部允许（A 键）

    @staticmethod
    def _format_observation(cmd: str, exec_result: dict) -> str:
        """委派给 tool_dispatch.format_observation"""
        return tool_dispatch.format_observation(cmd, exec_result)

    def _run_llm_once(self, skill: Skill, arguments: dict, llm) -> dict:
        """档位 A：单次 LLM 调用（CC 原生 skill 兜底）

        工作流程：
        1. 编译 final prompt（Assembler 处理 !cmd 预处理）
        2. 调用 LLM 一次（不 loop，不 parse tool_call）
        3. 返回 LLM 输出

        适用于：leetcode-solution-writer 原版（fetch → LLM compose → save）
        不适用于：grill-me / algorithm-interviewer（需要 tool_dispatch loop）
        """
        final_prompt = self.assembler.assemble(skill, arguments)

        try:
            resp = llm.invoke(final_prompt)
            # LangChain invoke 返回 BaseMessage，取 content 字符串
            if hasattr(resp, "content"):
                output = resp.content if isinstance(resp.content, str) else str(resp.content)
            else:
                output = str(resp)
            return {
                "skill_name": skill.metadata.name,
                "score": 1.0,
                "steps": [{"name": "llm_once", "type": "llm", "output": output}],
                "output": output,
                "files_created": [],
            }
        except Exception as e:
            return {
                "skill_name": skill.metadata.name,
                "score": 1.0,
                "steps": [{"name": "llm_once", "type": "llm", "error": str(e)}],
                "output": f"[LLM 调用失败: {str(e)}]",
                "files_created": [],
            }

    def _run_steps(
        self,
        steps: list[Step],
        arguments: dict,
        skill: Skill,
    ) -> dict:
        """按步骤执行 — 委派给 StepsRunner"""
        s_runner = steps_runner.StepsRunner(
            executor=self.executor,
            approval_fn=self._check_approval,
        )
        return s_runner.run(steps, arguments, skill)

    def _execute_step(
        self,
        step: Step,
        prev_outputs: dict,
        arguments: dict,
        skill: Skill,
    ) -> dict:
        """执行单步 — 委派给 StepsRunner"""
        s_runner = steps_runner.StepsRunner(
            executor=self.executor,
            approval_fn=self._check_approval,
        )
        return s_runner._execute_step(step, prev_outputs, arguments, skill)

    # ================================================================
    # 安全审批辅助
    # ================================================================

    def _check_approval(self, skill_name: str, binary: str, op_str: str = "") -> bool:
        """检查操作是否已被审批，或弹交互式确认

        审批级别（按 op_str 粒度）：
        - y: 本次允许
        - Y: 当前会话允许（同 op_str 自动放行）
        - N: 拒绝
        - r: 当前会话拒绝（同 op_str 自动拒绝）
        - A: 全部允许（当前会话剩余所有操作自动放行）
        """
        import sys, os

        # 会话级全部允许
        if self._session_allow_all:
            return True

        # 会话级审批缓存
        if op_str in self._session_approvals:
            return self._session_approvals[op_str]

        # 非交互模式
        auto_approve = os.environ.get("SKILLS_ENGINE_AUTO_APPROVE", "").strip().lower()
        NO_AUTO = {"", "none", "0", "false"}
        if auto_approve and auto_approve not in NO_AUTO:
            if auto_approve == "all":
                return True
            for entry in auto_approve.split(","):
                entry = entry.strip()
                if ":" in entry:
                    s, b = entry.split(":", 1)
                    if s == skill_name and b == binary:
                        return True
                elif entry == skill_name:
                    return True
            return False

        # 测试/CI 环境默认拒绝
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("CI"):
            return False

        # ===== 交互模式 =====
        try:
            import ctypes
            from ctypes import wintypes
            _kernel32 = ctypes.windll.kernel32

            _stdin_handle = _kernel32.GetStdHandle(-10)
            _stdout_handle = _kernel32.GetStdHandle(-11)
            INVALID = ctypes.c_void_p(-1).value

            if _stdin_handle in (None, INVALID, 0):
                return False
            if _stdout_handle in (None, INVALID, 0):
                _stdout_handle = _kernel32.GetStdHandle(-12)
            if _stdout_handle in (None, INVALID, 0):
                return False

            # 保存并设置控制台输入模式（禁用行缓冲和回显）
            _old_mode = wintypes.DWORD(0)
            _kernel32.GetConsoleMode(_stdin_handle, ctypes.byref(_old_mode))
            _raw_mode = _old_mode.value & ~0x0006
            _kernel32.SetConsoleMode(_stdin_handle, _raw_mode)

            # 输出提示
            prompt_text = f"\n⚠️  [{skill_name}] 请求执行:\n"
            prompt_text += f"   命令: {op_str or binary}\n"
            prompt_text += "   本次允许(y) / 会话允许(Y) / 拒绝(N) / 会话拒绝(r) / 全部允许(A): "
            _kernel32.WriteConsoleW(
                _stdout_handle, prompt_text, len(prompt_text), None, None
            )

            # 读单个字符
            _buf = ctypes.create_unicode_buffer(2)
            _read = wintypes.DWORD(0)
            _kernel32.ReadConsoleW(
                _stdin_handle, _buf, 1, ctypes.byref(_read), None
            )
            _raw = _buf.value
            _kernel32.WriteConsoleW(_stdout_handle, "\n", 1, None, None)

            # 恢复原始控制台模式
            _kernel32.SetConsoleMode(_stdin_handle, _old_mode)

        except Exception:
            return False

        # 区分大小写处理
        if _raw == "y":
            return True  # 本次允许，不记缓存
        elif _raw == "Y":
            self._session_approvals[op_str] = True
            return True  # 会话允许
        elif _raw == "N":
            return False  # 拒绝本次，不记缓存
        elif _raw == "r":
            self._session_approvals[op_str] = False
            return False  # 会话拒绝
        elif _raw == "A":
            self._session_allow_all = True
            return True  # 全部允许
        else:  # 其他按键
            return False
    # ================================================================
    # Phase 8: 档位 B — tool_dispatch loop
    # ================================================================

    def _parse_steps_from_body(self, body: str) -> Optional[list[Step]]:
        """从 SKILL.md body 中解析 ## Steps — 委派给 steps.parse_steps_from_body"""
        return steps_runner.parse_steps_from_body(body)

    def run(
        self,
        match_result: MatchResult,
        steps: Optional[list[Step]] = None,
        llm: Optional[object] = None,
        tool_dispatch: Optional[object] = None,
        max_iterations: int = 10,
        working_root: Optional[str] = None,
        state_path: Optional[str] = None,
        resume_from: Optional[str] = None,
    ) -> dict:
        """Execute skill - 4-route dispatch (auto-detect Steps DSL).

        Args:
            match_result: Match result
            steps: Custom steps DSL (optional)
            llm: LLM client (optional, for route A)
            tool_dispatch: LLM client (optional, for route B)
            max_iterations: Max iterations (route B)
            working_root: Working directory root (file operations base, defaults to skill.directory)
            state_path: 可选，运行状态落盘路径（P2-3 todo 续跑）
            resume_from: 可选，从指定状态文件续跑（P2-3）
        """
        skill = match_result.skill
        arguments = match_result.arguments

        # Route 0: auto-detect Steps from body (highest priority)
        if steps is None:
            parsed = self._parse_steps_from_body(skill.body)
            if parsed is not None:
                steps = parsed

        # Route 1: explicit steps - deterministic execution
        if steps is not None:
            return self._run_steps(steps, arguments, skill)

        # Route 2: route B - tool_dispatch loop
        if tool_dispatch:
            return self._run_tool_dispatch(match_result, tool_dispatch, max_iterations, working_root,
                                            state_path=state_path, resume_from=resume_from)

        # Route 3: route A - single LLM call
        if llm:
            return self._run_llm_once(skill, arguments, llm)

        # Route 4: pure compile
        final_prompt = self.assembler.assemble(skill, arguments)
        return {
            "skill_name": skill.metadata.name,
            "score": match_result.score,
            "steps": [],
            "output": final_prompt,
            "files_created": [],
            "iterations": 0,
            "stopped_by": "none",
        }

    def run_plan(

                    self,
                    plan,
                    registry: "Registry",
                    query: str = "",
                    llm: Optional[object] = None,
                    tool_dispatch: Optional[object] = None,
                    max_iterations: int = 10,
                    working_root: Optional[str] = None,
                    state_path: Optional[str] = None,
                    resume_from: Optional[str] = None,
                ) -> dict:
                    """执行 MatchPlan（直接传入 MatchPlan，不经过 MatchResult 包装）

                    Args:
                        plan: MatchPlan 对象（含 primary/selections）
                        registry: Registry（用于 load_skill）
                        query: 用户原始输入
                        llm: LLM 客户端（档位 A）
                        tool_dispatch: LLM 客户端（档位 B）
                        max_iterations: 档位 B 最大迭代次数
                        working_root: 工作目录根路径（文件操作基准，默认 skill.directory）

                    Returns:
                        与 run() 相同的 dict 格式。
                        multi 模式时返回 all_outputs 列表。
                    """
                    from skill_engine.models import MatchResult

                    if plan.mode == "multi" and plan.selections:
                        all_results = []
                        for selected in plan.selections:
                            skill = registry.load_skill(selected.name)
                            if not skill:
                                continue
                            mr = MatchResult(
                                skill=skill, score=selected.score or plan.score or 1.0,
                                method=plan.method, arguments={"$ARGUMENTS": query, "$0": query, **parse_named_params(query)},
                            )
                            result = self.run(
                                mr, llm=llm, tool_dispatch=tool_dispatch, max_iterations=max_iterations,
                                working_root=working_root, state_path=state_path, resume_from=resume_from,
                            )
                            result["skill_name"] = selected.name
                            all_results.append(result)
                        return {
                            "skill_name": plan.selections[0].name,
                            "score": plan.score or 1.0,
                            "all_outputs": all_results,
                            "output": all_results[-1].get("output", "") if all_results else "",
                            "files_created": [],
                            "steps": [],
                        }

                    # single 模式
                    if not plan.primary:
                        return {"skill_name": "", "score": 0, "steps": [], "output": "", "files_created": []}

                    skill = registry.load_skill(plan.primary.name)
                    if not skill:
                        return {
                            "skill_name": plan.primary.name, "score": 0,
                            "steps": [], "output": f"[ERROR] 无法加载 skill: {plan.primary.name}",
                            "files_created": [],
                        }

                    mr = MatchResult(
                        skill=skill, score=plan.score or 1.0,
                        method=plan.method, arguments={"$ARGUMENTS": query, "$0": query, **parse_named_params(query)},
                    )
                    return self.run(mr, llm=llm, tool_dispatch=tool_dispatch, max_iterations=max_iterations,
                       working_root=working_root, state_path=state_path, resume_from=resume_from)

    def _run_tool_dispatch(
        self,
        match_result: MatchResult,
        llm,
        max_iterations: int = 10,
        working_root: Optional[str] = None,
        state_path: Optional[str] = None,
        resume_from: Optional[str] = None,
    ) -> dict:
        """档位 B：tool_dispatch 循环 — 委派给 ToolDispatchRunner"""
        # 读取 human_in_loop 配置（从 SKILL.md frontmatter 或 .skill-local.yaml）
        from skill_engine.models import TurnPolicy
        from skill_engine.execution.human_io import CliHumanIO

        human_io = None
        turn_policy = None
        if match_result.skill.metadata.human_in_loop:
            human_io = CliHumanIO()
            turn_policy = TurnPolicy(**(match_result.skill.metadata.turn_policy or {}))

        td_runner = tool_dispatch.ToolDispatchRunner(
            executor=self.executor,
            assembler=self.assembler,
            approval_fn=self._check_approval,
            human_io=human_io,
            turn_policy=turn_policy,
            working_root=working_root,
        )
        return td_runner.run(match_result, llm, max_iterations,
                              state_path=state_path, resume_from=resume_from)

    def run_repl(
        self,
        plan,
        registry,
        query: str = "",
        llm=None,
        max_iterations: int = 30,
        working_root: Optional[str] = None,
        state_path: Optional[str] = None,
        resume_from: Optional[str] = None,
        human_io=None,
    ) -> dict:
        """多轮会话（Session/REPL）模式：单 skill 持续执行。详见 docs/多轮会话模式-session-repl设计.md。"""
        from skill_engine.execution.human_io import CliHumanIO
        from skill_engine.models import MatchResult

        def _load_session_state(path):
            import json
            from pathlib import Path as _P
            try:
                p = _P(path)
                if not p.exists():
                    return None
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None

        def _build_match_result(skill, score, method, q):
            return MatchResult(
                skill=skill, score=score, method=method,
                arguments={"$ARGUMENTS": q, "$0": q, **parse_named_params(q)},
            )

        def _session_result(name, reason, output=""):
            return {"skill_name": name, "output": output, "files_created": [],
                    "iterations": 0, "stopped_by": reason}

        # 1. 解析单 skill（session 不支持 multi 协同）
        skill, score, method, err = self._resolve_session_skill(plan, registry, query)
        if err is not None:
            return err

        # 归一化工作目录：允许 Windows 用户传 Git Bash / WSL 风格路径（/d/x、/mnt/d/x）
        wr_native = to_native_path(working_root) if working_root else Path.cwd()
        wr = str(wr_native)
        if not wr_native.is_dir():
            print(f"[ERROR] 工作目录不存在: {working_root}\n        {native_path_hint(working_root)}")
            return _session_result(skill.metadata.name, "invalid_working_root")
        session = SkillSession(skill_name=skill.metadata.name, working_root=wr, state_path=state_path)

        # 2. 续接
        resumed = False
        if resume_from:
            loaded = _load_session_state(resume_from)
            if loaded is not None:
                session.messages = list(loaded.get("messages", []))
                resumed = bool(session.messages)  # 空历史等同全新会话，避免以空 messages 起轮
                print(f"[session] 已从 {resume_from} 续接（{len(session.messages)} 条历史）")

        # 3. 构造 runner：session 模式 human_io 始终提供（供 ask_user），turn_policy=None 禁用内部循环
        hio = human_io or CliHumanIO()
        td_runner = tool_dispatch.ToolDispatchRunner(
            executor=self.executor, assembler=self.assembler,
            approval_fn=self._check_approval, human_io=hio,
            turn_policy=None, working_root=wr,
        )

        print(f"[session] 进入 {skill.metadata.name} 持续会话（输入 /exit 或 /done 退出）")
        # 未给初始 query 且非续接：先打印该 skill 的用法提示，再等待用户第一条指令
        if not (query or "").strip() and not resumed:
            print(self._format_skill_hint(skill))
        save = state_path or session.state_path

        return self._repl_loop(
            session=session, skill=skill, score=score, method=method,
            query=query, llm=llm, max_iterations=max_iterations,
            td_runner=td_runner, hio=hio, save=save, resumed=resumed,
            build_match_result=_build_match_result, session_result=_session_result,
        )

    # 空输入（直接回车）时追加的续写指令：明确语义为「基于已有历史继续」，
    # 而不是回退重跑原始 query（否则会把已完成的第一轮请求重做一遍）。
    _CONTINUE_HINT = "继续（沿用上文，接着往下做；不要重头开始）"

    @staticmethod
    def _format_skill_hint(skill) -> str:
        """渲染 skill 的用法提示（session 未带初始 query 时展示）。

        全部字段用 getattr 兜底：不同 skill 的 frontmatter 填写程度不一，
        缺字段只是少一行提示，不应让会话起不来。
        """
        m = getattr(skill, "metadata", None)
        name = getattr(m, "name", "skill")
        lines = [f"─── {name} ───"]

        def _add(label, value):
            if value:
                lines.append(f"  {label}: {value}")

        _add("用途", getattr(m, "description", ""))
        _add("适用", getattr(m, "when_to_use", ""))
        _add("参数", getattr(m, "argument_hint", ""))
        named = getattr(m, "arguments", None) or []
        if named:
            _add("命名参数", "  ".join(f"--{a}=<值>" for a in named))
        tools = list(getattr(m, "allowed_tools", None) or [])
        mcp = list(getattr(m, "mcp_servers", None) or [])
        _add("预授权工具", ", ".join(tools) if tools else "")
        _add("MCP", ", ".join(mcp) if mcp else "")

        lines.append("")
        lines.append("  直接用自然语言描述要做的事即可，例如：")
        hint = getattr(m, "argument_hint", "")
        lines.append(f"    {hint}" if hint else "    给 src/xxx.py 加一个 foo() 函数并补上测试")
        lines.append("  会话内命令：/exit 或 /done 退出 · 直接回车 = 沿用上文继续")
        return "\n".join(lines)

    def _resolve_session_skill(self, plan, registry, query: str):
        """从 plan 解析出 session 要跑的单个 skill。

        Returns:
            (skill, score, method, err_result)；解析失败时 skill 为 None、err_result 为可直接返回的 dict。
        """
        sel = plan.primary or (plan.selections[0] if plan.selections else None)
        if not sel:
            print(f"[ERROR] 未找到匹配的 skill: {query}")
            return None, 0, "", {"skill_name": "", "output": "", "files_created": [],
                                 "iterations": 0, "stopped_by": "no_match"}
        # 兼容两种 plan 类型：
        #  - MatchResult（旧 API / 单测）：带 .skill / .score / .method
        #  - SelectedSkill（router 真实输出的 MatchPlan）：只有 .name
        if getattr(sel, "skill", None) is not None:
            skill_name = sel.skill.metadata.name
            score = getattr(sel, "score", None) or plan.score or 1.0
            method = getattr(sel, "method", None) or plan.method or "name"
        else:
            skill_name = sel.name
            score = plan.score or 1.0
            method = plan.method or "name"
        skill = registry.load_skill(skill_name)
        if not skill:
            print(f"[ERROR] 无法加载 skill: {skill_name}")
            return None, 0, "", {"skill_name": skill_name,
                                 "output": f"[ERROR] 无法加载 skill: {skill_name}",
                                 "files_created": [], "iterations": 0, "stopped_by": "load_failed"}
        return skill, score, method, None

    def _repl_loop(self, session, skill, score, method, query, llm, max_iterations,
                   td_runner, hio, save, resumed, build_match_result, session_result) -> dict:
        """session 主循环：每轮读指令 → 调 run(session_mode=True) → 交还控制权。"""
        iteration = 0
        last_output = ""
        while True:
            iteration += 1
            if iteration == 1 and not resumed and (query or "").strip():
                # 全新会话首轮且带了初始 query：直接起轮
                mr = build_match_result(skill, score, method, query)
                initial_messages = None
            else:
                # 无初始 query / 续接首轮 / 后续各轮：等待用户下条指令
                user_cmd = hio.read(prompt=f"[{skill.metadata.name}] > ")
                if user_cmd in ("/exit", "/done"):
                    return session_result(session.skill_name, "user_exit", output=last_output)
                cmd = user_cmd.strip()
                if cmd:
                    session.append_user(cmd)
                elif session.messages:
                    # 空输入：让 LLM 基于历史续写，而非重跑原始 query
                    session.append_user(self._CONTINUE_HINT)
                else:
                    # 尚无任何历史就直接回车：无从"继续"，重新提示而不是空跑一轮
                    print("[session] 请输入一条指令（/exit 或 /done 退出）")
                    iteration -= 1
                    continue
                initial_messages = session.messages
                mr = build_match_result(skill, score, method, cmd or query)

            try:
                result = td_runner.run(
                    mr, llm, max_iterations=max_iterations,
                    state_path=save, initial_messages=initial_messages,
                    session_mode=True, snapshot=session.snapshot,
                )
            except KeyboardInterrupt:
                print(f"[session] 已被 Ctrl+C 中断，状态已落盘（{save}），可用 --resume-from 续接。")
                return session_result(session.skill_name, "interrupted", output=last_output)

            session.messages = result.get("history") or session.messages
            out = result.get("output", "")
            if out:
                last_output = out
                print(f"[{skill.metadata.name}] {out}")
            stopped = result.get("stopped_by")
            if stopped in ("error", "rate_limited", "max_iterations", "no_match", "load_failed"):
                return result
            # session_turn_end / tool_stop → 继续等待下条指令
            continue

    def _parse_tool_calls(self, response) -> list:
        """委派给 tool_dispatch.parse_tool_calls"""
        return tool_dispatch.parse_tool_calls(response)


    def create_skill(
        self,
        intent: str = None,
        llm: object = None,
        name: str = None,
        dry_run: bool = False,
        description: str = None,
        groups: list[str] = None,
        when_to_use: str = "",
        argument_hint: str = "",
        arguments: list[str] = None,
        body_template: str = "",
        scripts: dict[str, str] = None,
        assets: dict[str, str] = None,
        steps: list = None,
        skills_dir: str = "skills",
    ) -> dict:
        """创建 skill — 委派给 creator.create_skill"""
        from skill_engine.creator.creator import create_skill as _create_skill
        return _create_skill(
            intent=intent, llm=llm, name=name, dry_run=dry_run,
            description=description, groups=groups, when_to_use=when_to_use,
            argument_hint=argument_hint, arguments=arguments,
            body_template=body_template, scripts=scripts, assets=assets,
            steps=steps, skills_dir=skills_dir,
            assembler=self.assembler, executor=self.executor,
        )

    def _default_body_template(self, name: str, description: str) -> str:
        """生成默认 body 模板 — 委派给 creator.default_body_template"""
        from skill_engine.creator.creator import default_body_template
        return default_body_template(name, description)

    def register_new_skill(self, skill_dir: str, skills_dir: str = "skills") -> Optional[str]:
        """热注册新 skill — 委派给 creator.register_new_skill"""
        from skill_engine.creator.creator import register_new_skill
        return register_new_skill(skill_dir, skills_dir)
