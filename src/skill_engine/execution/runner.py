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

import re
import time
import yaml
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool
from skill_engine.models import Skill, MatchResult, Step
from skill_engine.execution.assembler import Assembler
from skill_engine.security.scanner import should_approve, _is_approved, _is_blocked, _save_approval, _save_blocklist
from skill_engine.security.scanner import should_approve, _is_approved, _is_blocked, _save_approval, _save_blocklist
from skill_engine.execution.executor import Executor


# ================================================================
# 档位 B 内建工具定义
# ================================================================
# 这些工具通过 bind_tools 传给 LLM，让模型知道它可以调用哪些工具。
# 工具的实际执行由 _run_tool_dispatch 循环中的 self.executor / 文件操作完成。


@tool
def bash(command: str) -> str:
    """Execute a shell command and return stdout. If the output is very long, it will be truncated."""  # noqa: E501


@tool
def read_file(path: str) -> str:
    """Read the contents of a file at the given path (relative to the skill directory)."""  # noqa: E501


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file at the given path (relative to the skill directory)."""


TOOL_DISPATCH_TOOLS = [bash, read_file, write_file]


def _parse_named_params(query: str) -> dict:
    """从 query 中提取 named params（key=value 或 key:value 对）

    两种格式：
    - key=value（key 不限字符集，支持中文）：topic=DP 或 主题=DP
    - key:value（仅 ASCII 键，防中文冒号误伤）：topic:DP

    Args:
        query: 用户输入字符串

    Returns:
        {key: value, ...}，空 query 返回空 dict
    """
    if not query or not query.strip():
        return {}
    params = {}
    # key=value 格式，key 不限字符集
    for match in re.finditer(r'([^=\s]+)=(\S+)', query):
        key = match.group(1).strip()
        value = match.group(2).rstrip(',;')
        params[key] = value
    # key:value 格式，仅 ASCII 键（防中文冒号误伤）
    for match in re.finditer(r'([a-zA-Z]\w*):\s*(\S+)', query):
        key = match.group(1).lower()
        value = match.group(2).rstrip(',;')
        if key not in params:
            params[key] = value
    return params


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
        """按步骤执行（engine 原生 DSL，Phase 3 丰满）

        每步的输出可以作为下一步的 input_ref。
        """
        step_outputs = {}
        step_results = []
        files_created = []

        for step in steps:
            result = self._execute_step(step, step_outputs, arguments, skill)
            step_results.append(result)

            # 保存输出供后续步骤引用
            if "output" in result:
                step_outputs[step.name] = result["output"]

            # 记录创建的文件
            if "file_created" in result:
                files_created.append(result["file_created"])

            # llm 步骤后间隔 1 秒，防止触发 API RPM 限制
            if getattr(step, "type", None) == "llm":
                time.sleep(1)

        # 空 steps 列表返回空 output
        if not steps:
            return {
                "skill_name": skill.metadata.name,
                "score": 1.0,
                "steps": step_results,
                "output": "",
                "files_created": files_created,
            }

        return {
            "skill_name": skill.metadata.name,
            "score": 1.0,
            "steps": step_results,
            "output": step_outputs.get(steps[-1].name, ""),
            "files_created": files_created,
        }

    def _execute_step(
        self,
        step: Step,
        prev_outputs: dict,
        arguments: dict,
        skill: Skill,
    ) -> dict:
        """执行单步"""

        if step.type == "exec":
            return self._exec_step(step, prev_outputs, arguments, skill)
        elif step.type == "llm":
            return self._llm_step(step, prev_outputs, arguments, skill)
        elif step.type == "write":
            return self._write_step(step, prev_outputs, arguments, skill)
        elif step.type == "read":
            return self._read_step(step, prev_outputs, arguments, skill)
        else:
            return {"error": f"未知 step 类型: {step.type}"}

    def _exec_step(
        self, step: Step, prev_outputs: dict, arguments: dict, skill: Skill
    ) -> dict:
        """执行 shell 命令"""
        cmd = self._resolve_template(step.command or "", prev_outputs, arguments)

        # 安全审批
        decision, reason = should_approve(
            cmd, skill.directory, risk_hint="step_exec"
        )
        if decision == "BLOCK":
            return {"name": step.name, "type": "exec", "command": cmd,
                    "output": "", "error": f"[安全拦截] {reason}", "exit_code": 1, "timed_out": False}
        if decision == "ATTENTION":
            approved = self._check_approval(skill.metadata.name, cmd.split()[0] if cmd else "", cmd)
            if not approved:
                return {"name": step.name, "type": "exec", "command": cmd,
                        "output": "", "error": "[用户跳过] 操作已取消", "exit_code": 1, "timed_out": False}

        # 使用 step 级别的 timeout
        step_timeout = step.timeout
        result = self.executor.run_step(cmd, cwd=Path(skill.directory), timeout=step_timeout)
        return {
            "name": step.name,
            "type": "exec",
            "command": cmd,
            "output": result["stdout"],
            "error": result["stderr"],
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
        }

    def _llm_step(
        self, step: Step, prev_outputs: dict, arguments: dict, skill: Skill
    ) -> dict:
        """调用 LLM 生成内容（steps DSL 中的 llm 步骤）"""
        template = self._resolve_template(
            step.template or "", prev_outputs, arguments
        )
        # step 不需要关心model
        # model = step.model or self.llm_model

        # 使用 config.get_llm() 获取 LLM
        try:
            from skill_engine.config import get_llm

            llm = get_llm(temperature=0.7)

            # 直接调用 LLM，不使用 create_agent（避免额外依赖）
            resp = llm.invoke(template)
            if hasattr(resp, "content"):
                output = resp.content if isinstance(resp.content, str) else str(resp.content)
            else:
                output = str(resp)

            return {
                "name": step.name,
                "type": "llm",
                # "model": model,
                "output": output,
            }
        except Exception as e:
            return {
                "name": step.name,
                "type": "llm",
                # "model": model,
                "error": str(e),
                "output": f"[LLM 调用失败: {e}]",
            }

    def _write_step(
        self, step: Step, prev_outputs: dict, arguments: dict, skill: Skill
    ) -> dict:
        """写入文件"""
        content = self._resolve_template(
            step.template or prev_outputs.get("", ""), prev_outputs, arguments
        )
        output_file = self._resolve_template(step.output_file or "", prev_outputs, arguments)

        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(output_file).write_text(content, encoding="utf-8")

        return {
            "name": step.name,
            "type": "write",
            "file_created": output_file,
        }

    def _read_step(
        self, step: Step, prev_outputs: dict, arguments: dict, skill: Skill
    ) -> dict:
        """读取文件"""
        filepath = self._resolve_template(step.input_ref or "", prev_outputs, arguments)
        try:
            content = Path(filepath).read_text(encoding="utf-8")
            return {"name": step.name, "type": "read", "output": content}
        except FileNotFoundError:
            return {"name": step.name, "type": "read", "error": f"文件不存在: {filepath}"}

    def _resolve_template(
        self, template: str, prev_outputs: dict, arguments: dict
    ) -> str:
        """解析模板中的变量引用

        支持：
        - {variable} → prev_outputs[variable]
        - $VAR → arguments[VAR] 或 arguments[$VAR]
        """
        # 替换 {step_name} 引用
        for name, output in prev_outputs.items():
            template = template.replace(f"{{{name}}}", output)

        # 替换 $ 参数（先匹配完整 $KEY，再匹配 $KEY[N]）
        for key, value in arguments.items():
            if key.startswith("$"):
                # 直接匹配 $ARGUMENTS, $0, $1 等
                template = template.replace(key, str(value))
            else:
                # 命名参数，匹配 $name
                template = template.replace(f"${key}", str(value))
        # 替换 {var} 语法（与 assembler._substitute_params 对齐）
        for key, value in arguments.items():
            key_clean = key.lstrip("$")
            template = template.replace(f"{{{key_clean}}}", str(value))
        return template

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
        """
        import sys, os

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
            prompt_text += "   本次允许(y) / 会话允许(Y) / 拒绝(N) / 会话拒绝(r): "
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
        else:  # 其他按键
            return False
    # ================================================================
    # Phase 8: 档位 B — tool_dispatch loop
    # ================================================================

    def _parse_steps_from_body(self, body: str) -> Optional[list[Step]]:
        """从 SKILL.md body 中解析 ## Steps 部分的步骤定义。

        返回 Step 列表如果找到 ## Steps 部分，否则返回 None。

        解析格式（YAML-block 列表，每个 step 以 `- name:` 开头）：
        ```
        ## Steps

        - name: fetch_problem
          type: exec
          command: python scripts/fetch_problem.py $0
          timeout: 30

        - name: save_solution
          type: write
          output_file: output/49_solution.md
          template: |
            # 题解 49
        ```
        """
        import re

        match = re.search(r'^## Steps\s*\n(.*?)(?=^## |\Z)', body, re.MULTILINE | re.DOTALL)
        if not match:
            return None

        steps_text = match.group(1).strip()
        if not steps_text:
            return None

        steps = []
        # 分割每个 step block（以 "- name:" 开头）
        step_blocks = re.split(r'\n(?=- name:)', steps_text)

        for block in step_blocks:
            block = block.strip()
            if not block:
                continue
            # 移除开头的 "- name:" 标记，解析为 YAML
            try:
                step_dict = yaml.safe_load(block)
                # YAML 看到 "- name:" 会解析为列表 [dict]，需要解包
                if isinstance(step_dict, list) and len(step_dict) == 1:
                    step_dict = step_dict[0]
                if isinstance(step_dict, dict) and 'name' in step_dict:
                    steps.append(Step(**step_dict))
            except yaml.YAMLError:
                continue

        return steps if steps else None

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
                                method=plan.method, arguments={"$ARGUMENTS": query, "$0": query, **_parse_named_params(query)},
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
                        method=plan.method, arguments={"$ARGUMENTS": query, "$0": query, **_parse_named_params(query)},
                    )
                    return self.run(mr, llm=llm, tool_dispatch=tool_dispatch, max_iterations=max_iterations)

    def _run_tool_dispatch(

        self,
        match_result: MatchResult,
        llm,
        max_iterations: int = 10,
    ) -> dict:
        """档位 B：tool_dispatch 循环（CC 原生 skill 兼容）

        工作流程：
        1. 编译 final prompt 作为 system message
        2. 调用 LLM(llm.invoke(messages)) → 返回 {content, tool_calls}
        3. 有 tool_calls → Executor 执行 → 追加 tool message → 回到 2
        4. 无 tool_calls → LLM 返回最终答案 → 结束

        适用于：grill-me / algorithm-interviewer 等对话型 CC 原生 skill
        """
        skill = match_result.skill
        final_prompt = self.assembler.assemble(skill, match_result.arguments)

        # 将内建工具绑定到裸模型上，使 LLM 能够返回 tool_calls
        # 如果 llm 没有 bind_tools 方法（如测试 Mock），直接使用原 llm 向后兼容
        if hasattr(llm, "bind_tools"):
            llm_with_tools = llm.bind_tools(TOOL_DISPATCH_TOOLS)
        else:
            llm_with_tools = llm

        messages = [
            {"role": "user", "content": final_prompt},
        ]

        iterations = 0
        step_results = []
        files_created = []

        for i in range(max_iterations):
            iterations += 1

            print(f"\n=== Iteration {iterations}/{max_iterations} ===")
            print(f"  Messages in history: {len(messages)} items")

            # 调用 LLM（带 rate limit 退避重试）
            resp = None
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    resp = llm_with_tools.invoke(messages)
                    break
                except Exception as e:
                    err_str = str(e)
                    # 检测到 429 rate limit 错误，退避重试
                    if "429" in err_str or "rate" in err_str.lower() or "quota" in err_str.lower() or "exhaust" in err_str.lower():
                        wait_time = 3 * (attempt + 1)  # 3s, 6s, 9s
                        time.sleep(wait_time)
                        if attempt == max_retries - 1:
                            # 重试耗尽，返回错误
                            return {
                                "skill_name": skill.metadata.name,
                                "score": 1.0,
                                "steps": step_results,
                                "output": f"[LLM 调用被限流（已重试 {max_retries} 次）: {err_str}]",
                                "files_created": files_created,
                                "iterations": iterations,
                                "stopped_by": "rate_limited",
                            }
                    else:
                        # 非限流错误，直接返回
                        return {
                            "skill_name": skill.metadata.name,
                            "score": 1.0,
                            "steps": step_results,
                            "output": f"[LLM 调用失败: {err_str}]",
                            "files_created": files_created,
                            "iterations": iterations,
                            "stopped_by": "error",
                        }

            # 每轮 LLM 调用之间加短暂延迟，降低触发 rate limit 的概率
            time.sleep(0.5)

            # 标准化 LLM 响应为 dict（兼容 LangChain AIMessage）
            if hasattr(resp, "tool_calls"):
                resp = {
                    "content": resp.content if hasattr(resp, "content") else str(resp),
                    "tool_calls": list(resp.tool_calls) if resp.tool_calls else [],
                }
            elif not isinstance(resp, dict):
                resp = {"content": str(resp), "tool_calls": []}

            # 解析 tool_calls
            tool_calls = self._parse_tool_calls(resp)

            print(f"  LLM response: content={len(resp.get('content', ''))} chars, tool_calls={len(tool_calls)}")
            if tool_calls:
                for tc in tool_calls:
                    print(f"    - {tc['type']}: {tc['input']}")

            if not tool_calls:
                # LLM 返回最终答案，停止
                messages.append({"role": "assistant", "content": resp.get("content", "")})
                step_results.append({"name": "llm_response", "type": "llm", "output": resp.get("content", "")})
                return {
                    "skill_name": skill.metadata.name,
                    "score": 1.0,
                    "steps": step_results,
                    "output": resp.get("content", ""),
                    "files_created": files_created,
                    "iterations": iterations,
                    "stopped_by": "stop",
                }

            # 有 tool_calls，执行每个
            # 将内部格式 {type, input, id} 转换为 LangChain 消息格式 {name, args, id}
            lc_tool_calls = []
            for tc in tool_calls:
                lc_tool_calls.append({
                    "id": tc["id"],
                    "name": tc["type"],
                    "args": tc["input"],
                })
            messages.append({
                "role": "assistant",
                "content": resp.get("content", ""),
                "tool_calls": lc_tool_calls,
            })

            for tc in tool_calls:
                if tc["type"] == "stop":
                    return {
                        "skill_name": skill.metadata.name,
                        "score": 1.0,
                        "steps": step_results,
                        "output": tc["input"].get("reason", "stopped"),
                        "files_created": files_created,
                        "iterations": iterations,
                        "stopped_by": "tool_stop",
                    }

                elif tc["type"] == "bash":
                    cmd = tc["input"].get("command", "")
                    # tool_dispatch: LLM 侧命令，直接 BLOCK（安全设计 v2）
                    import logging
                    logging.getLogger("skill_engine.runner").warning(
                        f"tool_dispatch bash 被安全拦截: {cmd[:80]}"
                    )
                    step_results.append({
                        "name": f"bash_{tc['id']}",
                        "type": "bash",
                        "command": cmd,
                        "output": "",
                        "error": "[安全拦截] tool_dispatch 命令不自动执行",
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": "bash",
                        "content": "[安全拦截] tool_dispatch 命令不自动执行",
                    })
                    continue

                elif tc["type"] == "read_file":
                    filepath = tc["input"].get("path", "")
                    full_path = Path(skill.directory) / filepath
                    try:
                        content = full_path.read_text(encoding="utf-8")
                        step_results.append({
                            "name": f"read_{tc['id']}",
                            "type": "read_file",
                            "path": str(full_path),
                            "output": content[:1000],
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": content,
                        })
                    except FileNotFoundError:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": f"[文件不存在: {filepath}]",
                        })

                elif tc["type"] == "write_file":
                    filepath = tc["input"].get("path", "")
                    content = tc["input"].get("content", "")
                    full_path = Path(skill.directory) / filepath
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(content, encoding="utf-8")
                    files_created.append(str(full_path))
                    step_results.append({
                        "name": f"write_{tc['id']}",
                        "type": "write_file",
                        "path": str(full_path),
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "ok",
                    })

                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"[未知工具类型: {tc['type']}]",
                    })

        # 达到最大迭代次数
        return {
            "skill_name": skill.metadata.name,
            "score": 1.0,
            "steps": step_results,
            "output": "[达到最大迭代次数]",
            "files_created": files_created,
            "iterations": iterations,
            "stopped_by": "max_iterations",
        }

    def _parse_tool_calls(self, response) -> list:
        """解析 LLM 响应中的 tool_calls

        兼容两种输入格式：
        - LangChain AIMessage: 有 .tool_calls 属性，元素格式 {name, args, id}
        - dict: {"content": "...", "tool_calls": [{"type", "input", "id"}]}
        - str: 纯文本（无 tool_calls）

        Returns:
            tool_calls 列表，统一格式为 {"id": str, "type": str, "input": dict}
        """
        if isinstance(response, str):
            return []

        # LangChain AIMessage: 有 .tool_calls 属性
        if hasattr(response, "tool_calls"):
            tool_calls_raw = list(response.tool_calls) if response.tool_calls else []
        elif isinstance(response, dict):
            tool_calls_raw = response.get("tool_calls", [])
        else:
            return []

        if not tool_calls_raw:
            return []

        tool_calls = []
        for tc in tool_calls_raw:
            # LangChain 格式: name->type, args->input
            # 兼容两种格式：
            #   旧格式: {"type": "bash", "input": {...}, "id": "..."}
            #   新格式: {"name": "bash", "args": {...}, "id": "...", "type": "tool_call"}
            tool_type = tc.get("type", tc.get("name", "unknown"))
            # 如果 type 是 "tool_call"（LangChain 占位值），用 name 作为工具名
            if tool_type == "tool_call":
                tool_type = tc.get("name", "unknown")
            tool_calls.append({
                "id": tc.get("id", f"call_{len(tool_calls)}"),
                "type": tool_type,
                "input": tc.get("input", tc.get("args", {})),
            })

        return tool_calls

    # ================================================================
    # Phase 10: 多 skill 编排（Orchestrator）
    # ================================================================

    def _build_skill_catalog(self, registry) -> str:
        """构建可用 skill 目录 prompt

        按 groups 分组展示，减少 catalog 体积：
        - 有组的 skill → 缩略展示（group 标题 + 成员简述）
        - 无组的 skill → 完整展示

        每个 skill 仅注入 frontmatter meta，不加载 body。

        Args:
            registry: Registry 实例

        Returns:
            格式化的 skill 目录字符串
        """
        groups = registry.get_groups()
        lines = ["## 可用技能清单\n"]

        for group_name, skill_names in groups.items():
            if group_name == "__ungrouped__":
                # 无组 skill：逐条展示
                for name in skill_names:
                    lines.extend(self._format_skill_entry(registry, name))
            else:
                # 有组 skill：缩略展示
                lines.append(f"### 分组: {group_name}")
                lines.append(f"- 包含 {len(skill_names)} 个技能:")
                for name in skill_names:
                    fm = registry.info_full(name)
                    desc = fm.get("description", "") if fm else ""
                    if desc and desc != name:
                        lines.append(f"  - **{name}**: {desc}")
                    else:
                        lines.append(f"  - **{name}**")
        return "\n".join(lines)

    def _format_skill_entry(self, registry, name: str) -> list[str]:
        """格式化单个 skill 条目（用于无组 skill）"""
        fm = registry.info_full(name)
        if not fm:
            return []

        lines = [f"- **{name}**"]
        desc = fm.get("description", "")
        if desc and desc != name:
            lines.append(f"  - 描述: {desc}")
        when_to_use = fm.get("when_to_use", "")
        if when_to_use:
            lines.append(f"  - 适用场景: {when_to_use}")
        arg_hint = fm.get("argument_hint", "")
        if arg_hint:
            lines.append(f"  - 参数提示: {arg_hint}")
        effort = fm.get("effort", "")
        if effort and effort != "inherit":
            lines.append(f"  - 努力级别: {effort}")
        return lines

    def _parse_orchestration_plan(self, llm_response: str) -> dict:
        """解析 LLM 返回的编排计划

        LLM 应该返回 JSON，格式：
        {
            "plan": [
                {"skill": "deploy", "args": {}, "description": "部署服务"},
                {"skill": "code-review", "args": {}, "description": "审查代码"}
            ],
            "reasoning": "先部署再审查"
        }

        如果返回的不是 JSON，直接返回空计划。
        """
        import json

        plan = {"plan": [], "reasoning": llm_response}

        try:
            data = json.loads(llm_response)
            if isinstance(data, dict):
                plan["plan"] = data.get("plan", [])
                plan["reasoning"] = data.get("reasoning", llm_response)
        except (json.JSONDecodeError, TypeError):
            pass

        return plan

    def _execute_chain(
        self,
        chain: list,
        registry,
        llm,
        prev_outputs: dict,
        max_steps: int = 10,
    ) -> dict:
        """执行编排链

        按顺序执行每个 step，中间结果传递给后续步骤。

        Args:
            chain: [{"skill": name, "args": {}, "description": "..."}, ...]
            registry: Registry 实例
            llm: LLM 客户端
            prev_outputs: 之前的输出（用于变量传递）
            max_steps: 最大执行步数

        Returns:
            编排结果
        """
        chain_results = []
        all_outputs = []
        files_created = []

        for i, step in enumerate(chain):
            if i >= max_steps:
                break

            skill_name = step.get("skill", "")
            step_args = step.get("args", {})
            step_desc = step.get("description", "")

            # 加载 skill
            skill = registry.load_skill(skill_name)
            if not skill:
                chain_results.append({
                    "skill": skill_name,
                    "status": "error",
                    "args": step_args,
                    "error": f"未找到 skill: {skill_name}",
                })
                all_outputs.append(f"[{skill_name}] 未找到")
                continue

            # 合并参数（step_args 优先，prev_outputs 补充）
            merged_args = {**prev_outputs, **step_args}

            # 编译 prompt
            prompt = self.assembler.assemble(skill, merged_args)

            # 调用 LLM 执行该 skill
            try:
                output = llm.invoke(prompt)
                chain_results.append({
                    "skill": skill_name,
                    "status": "success",
                    "description": step_desc,
                    "args": step_args,
                    "output_len": len(output),
                })
                all_outputs.append(f"## [{skill_name}] {step_desc}\n{output}")

                # 提取创建的文件（如果有）
                # 简化：假设 LLM 返回中包含文件信息
            except Exception as e:
                chain_results.append({
                    "skill": skill_name,
                    "status": "error",
                    "error": str(e),
                })
                all_outputs.append(f"[{skill_name}] 执行失败: {e}")

        return {
            "chain": chain_results,
            "all_outputs": "\n\n---\n\n".join(all_outputs),
            "files_created": files_created,
        }

    def run_orchestration(
        self,
        user_input: str,
        registry,
        llm,
        max_planning_steps: int = 5,
        max_chain_steps: int = 10,
    ) -> dict:
        """LLM 编排多 skill 协同执行

        工作流程：
        1. 构建 skill 目录（catalog）
        2. 调用 LLM，让它决定用哪些 skill、什么顺序
        3. 按编排依次执行各 skill
        4. 汇总输出

        适用于：用户提出复杂需求，需要多个 skill 协同完成
        """
        # 1. 构建 skill 目录
        catalog = self._build_skill_catalog(registry)

        # 2. 构建编排 prompt
        system_prompt = f"""你是一个技能编排器。你有以下技能可用：

{catalog}

你的任务是：
1. 理解用户的需求
2. 从可用技能中选择需要的
3. 编排调用顺序
4. 依次调用，传递中间结果

请以 JSON 格式返回你的决策：
{{
  "plan": [
    {{"skill": "skill名称", "args": {{}}, "description": "这一步的作用"}},
    ...
  ],
  "reasoning": "你为什么这样编排"
}}

如果不需要任何技能，直接回答用户即可：
{{
  "plan": [],
  "reasoning": "直接回答的原因"
}}

用户需求: {user_input}
"""

        # 3. LLM 做编排决策
        messages = [{"role": "user", "content": system_prompt}]
        planning_iterations = 0

        while planning_iterations < max_planning_steps:
            planning_iterations += 1
            resp = llm.invoke(messages)

            # 解析 LLM 响应（可能是 dict 或 str）
            if isinstance(resp, dict):
                resp_text = resp.get("content", "")
            else:
                resp_text = str(resp)

            plan = self._parse_orchestration_plan(resp_text)
            chain = plan.get("plan", [])

            if not chain:
                # LLM 决定不调用任何 skill，直接回答
                return {
                    "skill_name": "orchestrator",
                    "score": 1.0,
                    "chain": [],
                    "reasoning": plan.get("reasoning", ""),
                    "output": resp_text if resp_text else str(resp),
                    "files_created": [],
                    "iterations": planning_iterations,
                    "stopped_by": "direct_answer",
                }

            # 4. 执行编排链
            result = self._execute_chain(
                chain, registry, llm, {}, max_steps=max_chain_steps
            )

            # 5. 汇总输出
            final_output = f"# 编排结果\n\n"
            final_output += f"**推理**: {plan.get('reasoning', '')}\n\n"
            final_output += f"**执行链**:\n"
            for step_result in result["chain"]:
                status = "✅" if step_result["status"] == "success" else "❌"
                final_output += f"  {status} {step_result['skill']}: {step_result.get('description', '')}\n"
            final_output += f"\n---\n\n{result['all_outputs']}\n"

            return {
                "skill_name": "orchestrator",
                "score": 1.0,
                "chain": result["chain"],
                "reasoning": plan.get("reasoning", ""),
                "output": final_output,
                "files_created": result["files_created"],
                "iterations": planning_iterations,
                "stopped_by": "complete",
            }

        # 达到最大规划步骤
        return {
            "skill_name": "orchestrator",
            "score": 1.0,
            "chain": [],
            "reasoning": "达到最大规划步骤",
            "output": "[达到最大规划步骤]",
            "files_created": [],
            "iterations": planning_iterations,
            "stopped_by": "max_planning_steps",
        }

    # Phase 11+12: Skill 自动创建 + 验证 + 热注册
    # ================================================================

    def create_skill(
        self,
        # LLM 模式参数
        intent: Optional[str] = None,
        llm: Optional[object] = None,
        # 通用参数
        name: Optional[str] = None,
        dry_run: bool = False,
        # 直接模式参数（向后兼容）
        description: Optional[str] = None,
        groups: Optional[list[str]] = None,
        when_to_use: str = "",
        argument_hint: str = "",
        arguments: Optional[list[str]] = None,
        body_template: str = "",
        scripts: Optional[dict[str, str]] = None,
        assets: Optional[dict[str, str]] = None,
        steps: Optional[list] = None,
        skills_dir: str = "skills",
    ) -> dict:
        """创建 skill — 双模式

        LLM 模式（Phase 11 主模式）：
            传入 intent + llm → LLM 自动生成完整设计

        直接模式（向后兼容）：
            传入 name + description + ... → 直接调 Creator 写入

        Args:
            intent: 自然语言意图（LLM 模式）
            llm: LLM 客户端（LLM 模式）
            name: 可选，覆盖 LLM 生成的名称（LLM 模式）或必填（直接模式）
            dry_run: 仅生成 design 不写入（LLM 模式）
            description/groups/etc: 直接模式参数

        Returns:
            {name, path, status, valid, errors, ...}
        """
        from skill_engine.creator.creator import SkillCreator, SkillValidator
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        # LLM 模式
        if intent is not None and llm is not None:
            from skill_engine.creator.designer import SkillDesigner

            designer = SkillDesigner()
            design = designer.design(intent, llm)

            if name:
                design["name"] = name

            if dry_run:
                return {"valid": True, "design": design, "dry_run": True}

            VALID_CREATE_KEYS = {
                "name", "description", "groups", "when_to_use",
                "argument_hint", "arguments", "body_template",
                "scripts", "assets", "steps",
            }
            filtered = {k: v for k, v in design.items() if k in VALID_CREATE_KEYS}

            creator = SkillCreator(base_dir=skills_dir)
            result = creator.create(**filtered)
            result["name"] = design["name"]

            if result["status"] == "success":
                index = discover(roots=[skills_dir])
                registry = Registry(index)
                skill = registry.load_skill(design["name"])
                if skill:
                    validator = SkillValidator(self.assembler, self.executor)
                    validation = validator.full_validate(skill)
                    result["validated"] = True
                    result["compile_result"] = validation["compile"]
                    result["scripts_result"] = validation["scripts"]
                    result["valid"] = validation["valid"]
                else:
                    result["errors"].append("注册失败：无法加载新 skill")
                    result["status"] = "failed"
                    result["valid"] = False
            else:
                result["validated"] = False
                result["valid"] = False

            return result

        # 直接模式（向后兼容）
        assert name is not None, "直接模式需要提供 name 参数"

        creator = SkillCreator(base_dir=skills_dir)
        bt = body_template
        if not bt and (steps or arguments):
            bt = ""  # 触发结构化 body
        elif not bt:
            bt = self._default_body_template(name, description or "")

        result = creator.create(
            name=name,
            description=description or "",
            groups=groups,
            when_to_use=when_to_use,
            argument_hint=argument_hint,
            arguments=arguments,
            body_template=bt,
            scripts=scripts,
            assets=assets,
            steps=steps,
        )

        if result["status"] == "success":
            index = discover(roots=[skills_dir])
            registry = Registry(index)
            skill = registry.load_skill(name)
            if skill:
                validator = SkillValidator(self.assembler, self.executor)
                validation = validator.full_validate(skill)
                result["validated"] = True
                result["compile_result"] = validation["compile"]
                result["scripts_result"] = validation["scripts"]
                result["valid"] = validation["valid"]
            else:
                result["errors"].append("注册失败：无法加载新 skill")
                result["status"] = "failed"
                result["valid"] = False
        else:
            result["validated"] = False
            result["valid"] = False

        return result

    def _default_body_template(self, name: str, description: str) -> str:
        """生成默认 body 模板"""
        return f"""\
# {name}

{description}

## 工作流程

1. 理解用户需求
2. 执行相关操作
3. 返回结果

## 注意事项

- 遵循技能设计规范
- 保持输出格式清晰
"""

    def register_new_skill(self, skill_dir: str, skills_dir: str = "skills") -> Optional[str]:
        """热注册新 skill 到 registry（无需重启）

        直接从磁盘 discover 并添加到 index。

        Args:
            skill_dir: skill 目录名（相对于 skills_dir）

        Returns:
            skill name 或 None
        """
        from skill_engine.routing.discovery import discover, _discover_skill_dir
        from skill_engine.routing.registry import Registry

        full_dir = Path(skills_dir) / skill_dir
        if not full_dir.exists():
            return None

        # 重新 discover
        index = discover(roots=[skills_dir])
        name = None
        for n, meta in index.items():
            if meta.directory == str(full_dir):
                name = n
                break

        return name
