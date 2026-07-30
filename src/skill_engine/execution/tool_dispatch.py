"""档位 B：Tool Dispatch 循环（Agent Loop）

职责：
1. 编译 final prompt（Assembler）
2. 绑定工具（bind_tools）
3. LLM 循环调用  解析 tool_calls  执行  追加 tool message  回到 3
4. 无 tool_calls  返回最终答案

与 Runner 的交互：
- 通过 approval_fn 回调（Runner._check_approval）处理安全审批
- 直接使用 executor 和 assembler 实例
"""

import os
import time
import logging
from pathlib import Path
from typing import Optional, Callable

from skill_engine.models import Skill, MatchResult, TurnPolicy, RunResult
from skill_engine.execution.assembler import Assembler
from skill_engine.execution.executor import Executor
from skill_engine.execution.tool_defs import TOOL_REGISTRY, load_skill_tools
from skill_engine.execution.context_manager import ContextManager
from skill_engine.execution.human_io import HumanIO
from skill_engine.execution.snapshot import FileSnapshot
from skill_engine.security.scanner import should_approve
from langchain_core.tools import tool

import re
import json


# ---------------------------------------------------------------------------
# 编辑应用：精确匹配优先，失败走行级宽松模糊匹配（P2-1）
# ---------------------------------------------------------------------------
def _norm_ws(line: str) -> str:
    """行内多空白归一化为单空格 + 去首尾空白"""
    return re.sub(r"\s+", " ", line).strip()


def _fuzzy_find(content: str, old: str):
    """在 content 中按"连续行窗口"做宽松匹配，返回唯一候选的 (start, end) 字符区间。

    两级比较：先整行 strip，再归一化空白；任一唯一命中即返回。
    0 个或 >1 个候选都返回 None（无法唯一确定，交由上层报错）。
    """
    old_lines = old.split("\n")
    n = len(old_lines)
    if n == 0:
        return None
    # 预计算 content 每行的起止字符索引（兼容 \\n；\\r\\n 的行尾空白由 strip 处理）
    lines = []
    start = 0
    for line in content.split("\n"):
        lines.append((start, start + len(line), line))
        start += len(line) + 1  # +1 为一个换行符

    def candidates(transform):
        target = [transform(l) for l in old_lines]
        out = []
        for i in range(len(lines) - n + 1):
            window = [transform(lines[i + k][2]) for k in range(n)]
            if window == target:
                out.append((lines[i][0], lines[i + n - 1][1]))
        return out

    for transform in (lambda x: x.strip(), _norm_ws):
        c = candidates(transform)
        if len(c) == 1:
            return c[0]
    return None


def _apply_edits(content: str, edits: list) -> tuple:
    """对 content 应用 edits，精确匹配优先，失败走行级宽松模糊匹配。

    Returns:
        (new_content, None)            成功
        (None, error_message)          失败（error 信息会回传给 LLM 以便重试）
    """
    if not edits:
        return None, "error: edits 列表为空"
    for edit in edits:
        if not edit.get("oldText"):
            return None, "error: edit 项缺少 oldText"
    # 重复歧义：模糊匹配无法消除，直接报错（与原行为一致）
    for e in edits:
        c = content.count(e["oldText"])
        if c > 1:
            return None, f"error: oldText 在文件中出现 {c} 次（需唯一）: {e['oldText'][:80]}"
    # 全精确：每个 oldText 恰出现 1 次
    exact = all(content.count(e["oldText"]) == 1 for e in edits)
    if exact:
        positioned = sorted(edits, key=lambda e: content.find(e["oldText"]))
        search_pos = 0
        out = []
        for e in positioned:
            idx = content.find(e["oldText"], search_pos)
            out.append(content[search_pos:idx])
            out.append(e["newText"])
            search_pos = idx + len(e["oldText"])
        return "".join(out) + content[search_pos:], None
    # 模糊：至少一处不存在，逐 edit 锁定区间（基于原始 content，避免串扰）
    segments = []
    for e in edits:
        old = e["oldText"]
        if content.count(old) == 1:
            idx = content.find(old)
            segments.append((idx, idx + len(old), e["newText"]))
        else:
            m = _fuzzy_find(content, old)
            if m is None:
                return None, f"error: oldText 不存在（精确与模糊匹配均失败）: {old[:80]}"
            segments.append((m[0], m[1], e["newText"]))
    segments.sort(key=lambda s: s[0])
    out = []
    pos = 0
    for s, en, new in segments:
        out.append(content[pos:s])
        out.append(new)
        pos = en
    return "".join(out) + content[pos:], None


def _resolve_path(filepath: str, base_dir: Path) -> Path:
    """解析文件路径：绝对路径透传，否则基于 base_dir

    Args:
        filepath: 用户/LLM 传入的路径字符串
        base_dir: 基准目录（working_root 或 skill.directory）

    Returns:
        解析后的 Path 对象
    """
    p = Path(filepath)
    if p.is_absolute():
        return p
    # 展开 ~（如 ~/.ssh/config）
    expanded = os.path.expanduser(filepath)
    if expanded != filepath:
        return Path(expanded)
    # 相对路径
    return Path(base_dir) / filepath


def _read_file_with_lines(content: str, offset: int = 0, limit: int = 0) -> str:
    """读取文件内容，带行号，可选分页

    Args:
        content: 文件原文
        offset: 起始行号（0-indexed），0=从头
        limit: 返回行数，0=不限

    Returns:
        带行号的文件内容字符串
    """
    lines = content.splitlines(keepends=False)
    total = len(lines)

    if offset == 0 and limit == 0:
        # 无参数：返回全文 + 行号
        numbered = "\n".join(f"{i+1}:{line}" for i, line in enumerate(lines))
        if total > 200:
            numbered += f"\n(file has {total} lines, pass offset=0&limit={total} to read all)"
        return numbered

    # 显式分页
    start = offset
    end = offset + limit if limit else total
    snippet = lines[start:end]
    numbered = "\n".join(f"{i+1}:{line}" for i, line in enumerate(snippet, start=start))
    if end < total:
        numbered += f"\n(truncated, showing lines {start+1}-{end} of {total})"
    return numbered


def format_observation(cmd: str, exec_result: dict) -> str:
    """格式化 bash 执行结果为结构化 observation，包含 exit_code 等关键字段

    让 LLM 能区分"成功"（exit_code: 0）和"失败"（exit_code: 非零），
    避免空 stdout 时 LLM 无谓重试。

    Args:
        cmd: 原始命令
        exec_result: executor.run_step() 返回的结果 dict

    Returns:
        格式化后的 observation 字符串（≤10000 chars）
    """
    exit_code = exec_result.get("exit_code", -1)
    stdout = exec_result.get("stdout", "")
    stderr = exec_result.get("stderr", "")
    timed_out = exec_result.get("timed_out", False)

    lines = [f"exit_code: {exit_code}"]
    if timed_out:
        lines.append("(timed_out)")
    if stdout:
        lines.append("stdout:")
        lines.append(stdout[:8000])
    if stderr:
        lines.append("stderr:")
        lines.append(stderr[:500])
    return "\n".join(lines)[:10000]


def parse_tool_calls(response) -> list:
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


class ToolDispatchRunner:
    """档位 B：tool_dispatch 循环（CC 原生 skill 兼容）

    工作流程：
    1. 编译 final prompt 作为 system message
    2. 调用 LLM(llm.invoke(messages))  返回 {content, tool_calls}
    3. 有 tool_calls  Executor 执行  追加 tool message  回到 2
    4. 无 tool_calls  判断 human_in_loop  问用户 / 结束
    """

    def __init__(
        self,
        executor: Executor,
        assembler: Assembler,
        approval_fn: Optional[Callable] = None,
        human_io: Optional[HumanIO] = None,
        turn_policy: Optional[TurnPolicy] = None,
        working_root: Optional[str] = None,
    ):
        self.executor = executor
        self.assembler = assembler
        self.approval_fn = approval_fn  # Runner._check_approval 回调
        self.human_io = human_io
        self.turn_policy = turn_policy
        self.working_root = Path(working_root) if working_root else None

    def _truncate_msg(self, content: str, max_chars: int = 5000) -> str:
        """Truncate tool result message content to prevent context overflow.

        Full content is preserved in step_results for logging.
        """
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n...(truncated, {len(content)} chars total, showing first {max_chars})"

    def _check_file_safety(self, op_type: str, filepath: str, skill: Skill) -> tuple[bool, str]:
        """检查文件操作的安全性

        Args:
            op_type: 操作类型（read/write/edit）
            filepath: 目标文件路径
            skill: 当前 skill

        Returns:
            (approved, error_message)
        """
        from skill_engine.security.scanner import RISKY_FILENAMES
        # 直接检查文件名（不依赖 _path_escapes 的正则提取）
        if Path(filepath).name in RISKY_FILENAMES:
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, op_type, filepath)
            else:
                approved = False
            if not approved:
                return False, "[用户跳过] 敏感文件操作已取消"

        decision, reason = should_approve(
            f"{op_type}:{filepath}", skill.directory, risk_hint="tool_file"
        )
        if decision == "BLOCK":
            return False, f"[安全拦截] {reason}"
        elif decision == "ATTENTION":
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, op_type, filepath)
            else:
                approved = False
            if not approved:
                return False, "[用户跳过] 操作已取消"
        return True, ""

    def run(
        self,
        match_result: MatchResult,
        llm,
        max_iterations: int = 10,
        state_path: Optional[str] = None,
        resume_from: Optional[str] = None,
    ) -> dict:
        """执行 tool_dispatch 循环

        Args:
            match_result: 匹配结果
            llm: LLM 客户端（需支持 bind_tools）
            max_iterations: 最大迭代次数
            state_path: 可选，运行状态落盘路径（JSON）。每轮结束后写入，
                供后续 resume_from 续跑。不传则不持久化。
            resume_from: 可选，从指定状态文件续跑（载入 messages / 进度），
                继续对话而非重头来过。与 state_path 同源时即为"中断后续跑"。

        Returns:
            执行结果 dict
        """
        skill = match_result.skill
        final_prompt = self.assembler.assemble(skill, match_result.arguments)

        # P2-2：通用文件快照（检查点），记录写文件前的原始内容供回滚
        base_dir = self.working_root or Path(skill.directory)
        self._snapshot = FileSnapshot(base_dir)

        # 合并内建工具 + 该 skill 自带的领域工具，再按 allowed/disallowed 过滤
        skill_tools_map: dict[str, object] = {}
        if hasattr(llm, "bind_tools"):
            skill_extra = load_skill_tools(skill)
            # P2-2：内置 restore_file 工具（通用文件检查点回滚，不依赖 git）
            snap = self._snapshot

            @tool
            def restore_file(path: str) -> str:
                """回滚文件到本次运行开始前的快照状态（通用文件检查点，不依赖 git）。

                引擎在每次修改文件前会自动记录其原始内容。出错时可调用本工具
                将该文件恢复到修改前的状态。

                Args:
                    path: 要回滚的文件路径（绝对，或相对工作目录）
                """
                target = Path(path)
                if not target.is_absolute():
                    target = (base_dir / path).resolve()
                else:
                    target = target.resolve()
                ok, msg = snap.restore(target)
                return msg

            skill_extra_with_restore = skill_extra + [restore_file]
            skill_tools_map = {t.name: t for t in skill_extra_with_restore}
            tools = list(TOOL_REGISTRY.values()) + skill_extra_with_restore
            disallowed = getattr(skill.metadata, "disallowed_tools", None) or []
            allowed = getattr(skill.metadata, "allowed_tools", None) or []
            if disallowed:
                tools = [t for t in tools if t.name not in disallowed]
            if allowed:
                tools = [t for t in tools if t.name in allowed]
            llm_with_tools = llm.bind_tools(tools)
        else:
            llm_with_tools = llm

        # 上下文管理：token 预算 + 自动摘要压缩（大项目防止撑爆窗口）
        ctx = ContextManager(budget=getattr(skill.metadata, "context_budget", 0) or 8192)
        messages = ctx.messages

        iterations = 0
        step_results = []
        files_created = []

        # P2-3：todo 落盘续跑 —— 若给定 resume_from，载入上次运行状态继续对话
        save_path = state_path or resume_from
        if resume_from:
            loaded = self._load_state(resume_from)
            if loaded is not None:
                ctx.messages[:] = loaded.get("messages", [])
                messages = ctx.messages
                iterations = loaded.get("iterations", 0)
                step_results = loaded.get("step_results", [])
                files_created = loaded.get("files_created", [])
                # final_prompt 已含在 messages 中，不再重复追加
            else:
                messages.append({"role": "user", "content": final_prompt})
        else:
            messages.append({"role": "user", "content": final_prompt})

        result = None
        try:
            for i in range(max_iterations):
                iterations += 1

                print(f"\n=== Iteration {iterations}/{max_iterations} ===")
                print(f"  Messages in history: {len(messages)} items")

                # 上下文压缩：接近 token 预算时自动摘要压缩旧历史（保持首条与最近轮次）
                ctx.maybe_compress(llm)

                # 调用 LLM（带 rate limit 退避重试）
                resp = None
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        resp = llm_with_tools.invoke(messages)
                        break
                    except Exception as e:
                        err_str = str(e)
                        if "429" in err_str or "rate" in err_str.lower() or "quota" in err_str.lower() or "exhaust" in err_str.lower():
                            wait_time = 3 * (attempt + 1)
                            time.sleep(wait_time)
                            if attempt == max_retries - 1:
                                return RunResult(
                                    output=f"[LLM 调用被限流（已重试 {max_retries} 次）: {err_str}]",
                                    ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                         "iterations": iterations, "stopped_by": "rate_limited"},
                                    history=messages[:] if 'messages' in dir() else [],
                                )
                        else:
                            return RunResult(
                                output=f"[LLM 调用失败: {err_str}]",
                                ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                     "iterations": iterations, "stopped_by": "error"},
                                history=messages[:] if 'messages' in dir() else [],
                            )

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
                tool_calls = parse_tool_calls(resp)

                print(f"  LLM response: content={len(resp.get('content', ''))} chars, tool_calls={len(tool_calls)}")
                if tool_calls:
                    for tc in tool_calls:
                        print(f"    - {tc['type']}: {tc['input']}")

                if not tool_calls:
                    text = resp.get("content", "")
                    messages.append({"role": "assistant", "content": text})

                    if self.human_io and self.turn_policy:
                        # 多轮对话模式
                        if self.turn_policy.should_stop(text):
                            # LLM 说完了  直接结束，不追问用户
                            step_results.append({"name": "llm_response", "type": "llm", "output": text})
                            return RunResult(
                                output=text,
                                ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                     "iterations": iterations, "stopped_by": "stop"},
                                history=messages,
                            )
                        else:
                            # LLM 在问用户  emit + read + 追 history + 继续
                            self.human_io.emit(text)
                            user_input = self.human_io.read()

                            # 用户退出
                            if user_input in (self.turn_policy.user_exit or []):
                                step_results.append({"name": "llm_response", "type": "llm", "output": text})
                                return RunResult(
                                    output=text,
                                    ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                         "iterations": iterations, "stopped_by": "user_exit"},
                                    history=messages,
                                )

                            # 达到最大轮数
                            if iterations >= self.turn_policy.max_turns:
                                step_results.append({"name": "llm_response", "type": "llm", "output": text})
                                return RunResult(
                                    output=text,
                                    ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                         "iterations": iterations, "stopped_by": "max_turns"},
                                    history=messages,
                                )

                            # 追加用户回答，继续循环
                            messages.append({"role": "user", "content": user_input})
                            continue

                    # 非多轮模式：原行为
                    step_results.append({"name": "llm_response", "type": "llm", "output": text})
                    return RunResult(
                        output=text,
                        ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                             "iterations": iterations, "stopped_by": "stop"},
                        history=messages,
                    )

                # 有 tool_calls，执行每个
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
                        return RunResult(
                            output=tc["input"].get("reason", "stopped"),
                            ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                 "iterations": iterations, "stopped_by": "tool_stop"},
                            history=messages,
                        )

                    elif tc["type"] == "bash":
                        cmd = tc["input"].get("command", "")
                        decision, reason = should_approve(cmd, skill.directory, risk_hint="tool_dispatch")
                        if decision == "BLOCK":
                            logging.getLogger("skill_engine.tool_dispatch").warning(
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
                            print(f"     BLOCKED: {cmd[:80]}")
                            continue
                        elif decision == "ATTENTION":
                            if self.approval_fn:
                                approved = self.approval_fn(
                                    skill.metadata.name,
                                    cmd.split()[0] if cmd else "",
                                    cmd,
                                )
                            else:
                                approved = False
                            if not approved:
                                step_results.append({
                                    "name": f"bash_{tc['id']}",
                                    "type": "bash",
                                    "command": cmd,
                                    "output": "",
                                    "error": "[用户跳过] 操作已取消",
                                })
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "name": "bash",
                                    "content": "[用户跳过] 操作已取消",
                                })
                                print(f"     REJECTED by user: {cmd[:80]}")
                                continue
                        # SAFE 或审批通过：执行命令
                        try:
                            base_dir = self.working_root or Path(skill.directory)
                            exec_result = self.executor.run_step(cmd, cwd=base_dir)
                            obs = format_observation(cmd, exec_result)
                            step_results.append({
                                "name": f"bash_{tc['id']}",
                                "type": "bash",
                                "command": cmd,
                                "output": obs[:500],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "bash",
                                "content": self._truncate_msg(obs),
                            })
                            obs_lines = obs.split("\n")
                            print(f"     {obs_lines[0]}")
                            for ol in obs_lines[1:]:
                                if ol.strip():
                                    print(f"      {ol}")
                        except Exception as e:
                            step_results.append({
                                "name": f"bash_{tc['id']}",
                                "type": "bash",
                                "command": cmd,
                                "output": "",
                                "error": str(e),
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "bash",
                                "content": f"[执行失败: {e}]",
                            })
                            print(f"     ERROR: {e}")

                    elif tc["type"] == "read_file":
                        filepath = tc["input"].get("path", "")
                        offset = int(tc["input"].get("offset", 0))
                        limit = int(tc["input"].get("limit", 0))

                        # 安全门（只查路径，strict 不 BLOCK）
                        approved, err_msg = self._check_file_safety("read", filepath, skill)
                        if not approved:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "read_file",
                                "content": err_msg,
                            })
                            print(f"     {err_msg}")
                            continue

                        # 解析路径
                        base_dir = self.working_root or Path(skill.directory)
                        full_path = _resolve_path(filepath, base_dir)

                        try:
                            content = full_path.read_text(encoding="utf-8")
                            formatted = _read_file_with_lines(content, offset, limit)
                            step_results.append({
                                "name": f"read_{tc['id']}",
                                "type": "read_file",
                                "path": str(full_path),
                                "output": formatted[:1000],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "read_file",
                                "content": self._truncate_msg(formatted),
                            })
                            print(f"     read {len(formatted)} chars from {filepath}")
                        except FileNotFoundError:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "read_file",
                                "content": f"[文件不存在: {filepath}]",
                            })
                            print(f"     FILE NOT FOUND: {filepath}")
                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "read_file",
                                "content": f"[读取失败: {e}]",
                            })
                            print(f"     ERROR: {e}")

                    elif tc["type"] == "write_file":
                        filepath = tc["input"].get("path", "")
                        content = tc["input"].get("content", "")

                        # 安全门（只查路径，strict 不 BLOCK）
                        approved, err_msg = self._check_file_safety("write", filepath, skill)
                        if not approved:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "write_file",
                                "content": err_msg,
                            })
                            print(f"     {err_msg}")
                            continue

                        # 解析路径
                        base_dir = self.working_root or Path(skill.directory)
                        full_path = _resolve_path(filepath, base_dir)

                        try:
                            full_path.parent.mkdir(parents=True, exist_ok=True)
                            # P2-2：写回前记录快照（仅已存在文件，避免回滚到"已删除"状态）
                            if full_path.exists():
                                self._snapshot.record(full_path, full_path.read_text(encoding="utf-8"))
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
                                "name": "write_file",
                                "content": f"wrote {len(content)} bytes to {filepath}",
                            })
                            print(f"     wrote {len(content)} bytes to {filepath}")
                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "write_file",
                                "content": f"[写入失败: {e}]",
                            })
                            print(f"     ERROR: {e}")

                    elif tc["type"] == "edit_file":
                        filepath = tc["input"].get("path", "")
                        edits = tc["input"].get("edits", [])

                        if not edits:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": "error: edits 列表为空",
                            })
                            continue

                        # 安全门（只查路径，strict 不 BLOCK）
                        approved, err_msg = self._check_file_safety("edit", filepath, skill)
                        if not approved:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": err_msg,
                            })
                            print(f"     {err_msg}")
                            continue

                        # 解析路径
                        base_dir = self.working_root or Path(skill.directory)
                        full_path = _resolve_path(filepath, base_dir)

                        if not full_path.exists():
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": f"error: 文件不存在: {filepath}（新文件请用 write_file）",
                            })
                            print(f"     FILE NOT FOUND: {filepath}")
                            continue

                        try:
                            content = full_path.read_text(encoding="utf-8")

                            # 校验 + 应用：精确优先，oldText 不存在时走行级宽松模糊匹配（P2-1）
                            new_content, err = _apply_edits(content, edits)
                            if err:
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "name": "edit_file",
                                    "content": err,
                                })
                                print(f"     EDIT FAILED: {err[:80]}")
                                continue

                            # 写回前记录快照（P2-2：通用文件检查点，仅首次记录进入前状态）
                            self._snapshot.record(full_path, content)

                            # 写回文件
                            full_path.write_text(new_content, encoding="utf-8")
                            files_created.append(str(full_path))

                            result_msg = f"applied {len(edits)} edits to {filepath}"
                            step_results.append({
                                "name": f"edit_{tc['id']}",
                                "type": "edit_file",
                                "path": str(full_path),
                                "edits_count": len(edits),
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": result_msg,
                            })
                            print(f"     {result_msg}")

                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": f"[编辑失败: {e}]",
                            })
                            print(f"     ERROR: {e}")

                    elif tc["type"] == "search_files":
                        pattern = tc["input"].get("pattern", "")
                        search_path = tc["input"].get("path", ".")
                        file_glob = tc["input"].get("file_glob", "")
                        if not pattern:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "search_files",
                                "content": "error: pattern 不能为空",
                            })
                            continue
                        base_dir = self.working_root or Path(skill.directory)
                        search_dir = _resolve_path(search_path, base_dir)
                        if not search_dir.exists():
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "search_files",
                                "content": f"[路径不存在: {search_path}]",
                            })
                            continue
                        try:
                            import fnmatch
                            import re as re_module
                            matches = []
                            max_results = 50
                            total_size = 0
                            if search_dir.is_file():
                                files = [search_dir]
                            else:
                                files = list(search_dir.rglob("*"))
                                files.sort()
                            for f in files:
                                if len(matches) >= max_results:
                                    break
                                if not f.is_file():
                                    continue
                                if any(p.startswith(".") for p in f.parts):
                                    continue
                                if file_glob and not fnmatch.fnmatch(f.name, file_glob):
                                    continue
                                try:
                                    text = f.read_text(encoding="utf-8", errors="replace")
                                    for i, line in enumerate(text.splitlines(), 1):
                                        if re_module.search(pattern, line):
                                            rel = f.relative_to(search_dir) if search_dir.is_dir() else f.name
                                            match_line = f"{rel}:{i}: {line.strip()[:120]}"
                                            matches.append(match_line)
                                            total_size += len(match_line) + 1
                                            if total_size > 8000:
                                                matches.append("... (truncated, too many matches)")
                                                break
                                    if total_size > 8000:
                                        break
                                except (UnicodeDecodeError, PermissionError, OSError):
                                    continue
                            if not matches:
                                result = "no matches found"
                            else:
                                result = "\n".join(matches)
                            step_results.append({
                                "name": f"search_{tc['id']}",
                                "type": "search_files",
                                "pattern": pattern,
                                "path": str(search_dir),
                                "matches": len(matches),
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "search_files",
                                "content": self._truncate_msg(result),
                            })
                            print(f"     search '{pattern}' in {search_dir}: {len(matches)} matches")
                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "search_files",
                                "content": f"[搜索失败: {e}]",
                            })
                            print(f"     ERROR: {e}")

                    else:
                        # ---- skill 自带工具的通用执行分支（工具可插拔接口）----
                        # 内建工具名都已在上面的 if/elif 中处理，这里只兜底 skill 注入的领域工具。
                        # bind_tools 只负责"让 LLM 知道有这些工具"，真正执行需在此分支完成。
                        if tc["type"] in skill_tools_map:
                            tool_obj = skill_tools_map[tc["type"]]
                            base_dir = self.working_root or Path(skill.directory)
                            prev_cwd = os.getcwd()
                            try:
                                os.chdir(str(base_dir))  # 与 bash 一致的基准目录
                                result = tool_obj.invoke(tc["input"])
                            except Exception as e:
                                result = f"[工具执行失败: {e}]"
                            finally:
                                os.chdir(prev_cwd)
                            content = result if isinstance(result, str) else str(result)
                            step_results.append({
                                "name": f"skill_tool_{tc['id']}",
                                "type": tc["type"],
                                "output": content[:1000],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": tc["type"],
                                "content": self._truncate_msg(content),
                            })
                            print(f"     skill tool {tc['type']}: {len(content)} chars")
                            continue
                        # 既不是内建工具，也不是 skill 注入工具 → 真正未知
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": f"[未知工具类型: {tc['type']}]",
                        })

        # 达到最大迭代次数
            result = RunResult(
                output="[达到最大迭代次数]",
                ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                     "iterations": iterations, "stopped_by": "max_iterations"},
                history=messages,
            )
            return result
        finally:
            # P2-3：任一退出路径（含提前 stop / error / max_iterations）都落盘，支撑续跑
            if save_path:
                self._save_state(save_path, messages, iterations, step_results, files_created, final_prompt)
        return result

    # ---------------- P2-3：todo 落盘续跑（状态持久化） ----------------

    def _save_state(self, path, messages, iterations, step_results, files_created, final_prompt):
        """将运行状态落盘为 JSON，供后续 resume_from 续跑。失败静默。"""
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "final_prompt": final_prompt,
                "messages": messages,
                "iterations": iterations,
                "step_results": step_results,
                "files_created": files_created,
            }
            p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception:
            # 状态持久化失败绝不影响主执行流程
            pass

    def _load_state(self, path):
        """读取状态文件；不存在或损坏返回 None。"""
        try:
            p = Path(path)
            if not p.exists():
                return None
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None