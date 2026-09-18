"""bash：shell 命令执行（路由 + 安全审批 + 超时钳制 + 文件登记选择性失效）。

路由（见 security/sandbox.py:decide_route）：
- 只读白名单（ls/cat/grep/git status…，无重定向串联）→ 裸跑，省 ~0.35s/步
- 删除类命令（rm/del/rmdir…）→ 审批后**裸跑**：沙箱内 rm 会绕过宿主回收站守卫
- 联网 / 宿主工具（curl/uv/npm/ssh…）→ 审批后**裸跑**：沙箱内无网络、AppData 不可读
- 其余一切 → 进 srt 沙箱
"""

import logging

from skill_engine.execution.tool_exec.bash_util import (
    _extract_cmd_paths,
    format_observation,
)
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult
from skill_engine.security.sandbox import (
    ROUTE_ASK,
    ROUTE_SANDBOX,
    decide_route,
)
from skill_engine.security.scanner import should_approve

# bash 工具超时参数硬上限：测试/构建等长命令由 LLM 按需传 timeout，
# 引擎守住上限，防止失控命令拖垮会话。
BASH_MAX_TIMEOUT = 600


class BashHandler(BaseHandler):
    name = "bash"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        cmd = tc["input"].get("command", "")
        # debug 轨迹：实际执行的 shell 命令
        if ctx.tracer and ctx.tracer.enabled():
            ctx.tracer.event("command", cmd=cmd, tool_id=tc["id"])
        # 根目录用 ctx.base_dir（命令实际的 cwd），不用 skill.directory：
        # bash 跑在 base_dir 下，用 skill 目录做越界基准会把「工作区内但不在
        # skill 目录内」的合法路径全部误判为 ATTENTION。
        decision, reason = should_approve(
            cmd, str(ctx.base_dir), risk_hint="tool_dispatch", cwd=str(ctx.base_dir)
        )
        # 路由判定：删除类 / 联网类 / 宿主工具类命令必须审批后裸跑（详见模块 docstring）
        route, route_reason = decide_route(cmd)
        if ctx.tracer and ctx.tracer.enabled():
            ctx.tracer.event("sandbox_route", route=route, reason=route_reason)
        if route == ROUTE_ASK and decision == "SAFE":
            decision = "ATTENTION"
            reason = f"{reason}；{route_reason}" if reason else route_reason
        if decision == "BLOCK":
            # strict 快速失败：BLOCK 只会出现在 strict 模式下（LLM 侧 bash 一律不
            # 自动执行）。继续循环只会让模型一遍遍撞墙、空转耗尽迭代上限——
            # 直接终止本轮并明确告知原因。
            logging.getLogger("skill_engine.tool_dispatch").warning(
                f"tool_dispatch bash 被安全拦截（strict 快速失败）: {cmd[:80]}"
            )
            err = (
                "[安全拦截] 当前安全模式为 strict：LLM 发起的 bash 命令一律不自动执行。"
                "继续尝试 bash 只会耗尽迭代上限。请设置环境变量 "
                "SKILLS_ENGINE_SECURITY_MODE=permissive 后重试，"
                "或改用 read_file / write_file / edit_file / search_files 等文件工具。"
            )
            print(f"     BLOCKED (strict 快速失败): {cmd[:80]}")
            return ToolResult(
                tool_call_id=tc["id"], name="bash",
                content=err,
                step={
                    "name": f"bash_{tc['id']}",
                    "type": "bash",
                    "command": cmd,
                    "output": "",
                    "error": err,
                },
                hard_stop=True, stopped_by="security_blocked",
            )
        if decision == "ATTENTION":
            if ctx.approval_fn:
                approved = ctx.approval_fn(
                    ctx.skill.metadata.name,
                    cmd.split()[0] if cmd else "",
                    cmd,
                )
            else:
                approved = False
            if not approved:
                print(f"     REJECTED by user: {cmd[:80]}")
                return ToolResult(
                    tool_call_id=tc["id"], name="bash",
                    content="[用户跳过] 操作已取消",
                    step={
                        "name": f"bash_{tc['id']}",
                        "type": "bash",
                        "command": cmd,
                        "output": "",
                        "error": "[用户跳过] 操作已取消",
                    },
                )
        # SAFE 或审批通过：执行命令
        # LLM 可为测试/构建等长命令传 timeout（秒），引擎钳制到
        # BASH_MAX_TIMEOUT；不传则沿用 Executor 默认。
        try:
            req_timeout = int(tc["input"].get("timeout", 0) or 0)
        except (TypeError, ValueError):
            req_timeout = 0
        exec_timeout = min(req_timeout, BASH_MAX_TIMEOUT) if req_timeout > 0 else None
        # 只有「其余一切」进沙箱；删除/联网/宿主工具（已审批）与只读白名单裸跑。
        use_sandbox = route == ROUTE_SANDBOX
        try:
            exec_result = ctx.executor.run_step(
                cmd, cwd=ctx.base_dir, timeout=exec_timeout, sandbox=use_sandbox
            )
            # bash 可能改过文件 → 按命令中实际出现的路径选择性失效（文件级/目录级），
            # 未涉及的登记保留，消除「每次 bash 后全部文件回到未读」的迭代放大；
            # 无法提取路径 token 时保守全失效（与旧行为一致，如 echo hi）。
            touched = _extract_cmd_paths(cmd, ctx.base_dir)
            if touched is None:
                ctx.file_tracker.invalidate_all()
            else:
                ctx.file_tracker.invalidate_paths(touched)
            obs = format_observation(cmd, exec_result, sandbox=exec_result.get("sandbox"))
            # bash 真实输出走语义通道（行截断），替代裸 print 全打
            if ctx.emit_result:
                ctx.emit_result(obs)
            return ToolResult(
                tool_call_id=tc["id"], name="bash",
                content=ctx.truncate_msg(obs) if ctx.truncate_msg else obs,
                step={
                    "name": f"bash_{tc['id']}",
                    "type": "bash",
                    "command": cmd,
                    "output": obs[:500],
                },
            )
        except Exception as e:
            print(f"     ERROR: {e}")
            return ToolResult(
                tool_call_id=tc["id"], name="bash",
                content=f"[执行失败: {e}]",
                step={
                    "name": f"bash_{tc['id']}",
                    "type": "bash",
                    "command": cmd,
                    "output": "",
                    "error": str(e),
                },
            )
