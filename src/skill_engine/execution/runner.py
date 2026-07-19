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
    ) -> dict:
        """执行 skill — 四路分流（含 Steps DSL 自动检测）

        Args:
            match_result: 匹配结果
            steps: 自定义 steps DSL（可选）
            llm: LLM 客户端（可选，档位 A）
            tool_dispatch: LLM 客户端（可选，档位 B）
            max_iterations: 最大迭代次数（档位 B）
        """
        skill = match_result.skill
        arguments = match_result.arguments

        # 路径 0: 自动检测 body 中的 steps（优先级最高）
        if steps is None:
            parsed = self._parse_steps_from_body(skill.body)
            if parsed is not None:
                steps = parsed

        # 路径 1: 显式传入 steps → 确定性执行
        if steps is not None:
            return self._run_steps(steps, arguments, skill)

        # 路径 2: 档位 B — tool_dispatch loop（CC 原生 skill 兼容）
        if tool_dispatch:
            return self._run_tool_dispatch(match_result, tool_dispatch, max_iterations)

        # 路径 3: 档位 A — 单次 LLM 调用
        if llm:
            return self._run_llm_once(skill, arguments, llm)

        # 路径 4: 纯编译
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
                ) -> dict:
                    """执行 MatchPlan（直接传入 MatchPlan，不经过 MatchResult 包装）

                    Args:
                        plan: MatchPlan 对象（含 primary/selections）
                        registry: Registry（用于 load_skill）
                        query: 用户原始输入
                        llm: LLM 客户端（档位 A）
                        tool_dispatch: LLM 客户端（档位 B）
                        max_iterations: 档位 B 最大迭代次数

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
                                mr, llm=llm, tool_dispatch=tool_dispatch, max_iterations=max_iterations
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
                    return self.run(mr, llm=llm, tool_dispatch=tool_dispatch, max_iterations=max_iterations)

    def _run_tool_dispatch(
        self,
        match_result: MatchResult,
        llm,
        max_iterations: int = 10,
    ) -> dict:
        """档位 B：tool_dispatch 循环 — 委派给 ToolDispatchRunner"""
        td_runner = tool_dispatch.ToolDispatchRunner(
            executor=self.executor,
            assembler=self.assembler,
            approval_fn=self._check_approval,
        )
        return td_runner.run(match_result, llm, max_iterations)

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
