"""run_python：Python 代码执行工具。

替代 write_file 临时脚本 + bash python，或 bash python -c（cmd 引号易错）。
用临时文件 + executor（自动注入 venv）执行，规避 shell 引号转义。

临时文件位置：``<工作目录>/.skill-engine/tmp/``，**不是系统 temp**。
两个原因：
1. 沙箱用户读不了 ``C:\\Users\\<真实用户>\\AppData\\Local\\Temp``，脚本放系统
   temp 会让沙箱内的 python 直接找不到文件；
2. 临时脚本落在工作目录内，命中的是引擎自己的运行时目录，不污染仓库（该目录
   已被 .gitignore 覆盖），会话结束即清理。
"""

import os

from skill_engine.execution.paths import runtime_dir
from skill_engine.execution.tool_exec.bash_util import format_observation
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult
from skill_engine.security.sandbox import (
    ROUTE_ASK,
    ROUTE_SANDBOX,
    decide_route,
    detect_script_level_delete,
)
from skill_engine.security.scanner import should_approve

# 单段代码的审批理由展示长度上限
_OP_STR_MAX = 120


class RunPythonHandler(BaseHandler):
    name = "run_python"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        code = tc["input"].get("code", "").strip()
        try:
            timeout = int(tc["input"].get("timeout", 30))
        except (TypeError, ValueError):
            timeout = 30

        # 路由：默认进沙箱（P1 决定）。代码里出现越界路径 / 联网调用
        # （decide_route 的 ask 分支覆盖 curl/uv/ssh 等；越界路径由
        # should_approve 的 _path_escapes 负责）时升级为审批。
        route, route_reason = decide_route(code)
        # 本 handler 的入参是**裸 Python 代码**，首词通常是 top-level
        # import / 赋值 / with —— decide_route 里「首词 ∈ _PY_VERBS」的门控
        # 对它一次都不会命中，所以要单独补一次**不做门控**的扫描。
        # 不补的话 `shutil.rmtree('build')` 会被路由进沙箱，在沙箱内硬删、
        # 绕过宿主的回收站 / FAIL_CLOSED 守卫（比不开沙箱更危险）。
        delete_hit = detect_script_level_delete(code)
        if delete_hit and route == ROUTE_SANDBOX:
            route = ROUTE_ASK
            route_reason = (
                f"代码含删除等价物（{delete_hit}）：沙箱内删除绕过宿主守卫，需审批后裸跑"
            )
        decision, reason = should_approve(
            code, str(ctx.base_dir), risk_hint="tool_dispatch", cwd=str(ctx.base_dir)
        )
        if route == ROUTE_ASK and decision == "SAFE":
            decision = "ATTENTION"
            reason = f"{reason}；{route_reason}" if reason else route_reason
        if ctx.tracer and ctx.tracer.enabled():
            ctx.tracer.event("sandbox_route", tool="run_python", route=route, reason=route_reason)

        if decision == "BLOCK":
            obs = (
                "[安全拦截] 当前安全模式为 strict：LLM 发起的 run_python 一律不自动执行。"
                "请设置环境变量 SKILLS_ENGINE_SECURITY_MODE=permissive 后重试。"
            )
        elif decision == "ATTENTION":
            if ctx.approval_fn:
                approved = ctx.approval_fn(
                    ctx.skill.metadata.name,
                    "run_python",
                    f"run_python: {code[:_OP_STR_MAX]}",
                )
            else:
                approved = False
            if not approved:
                obs = "[用户跳过] 操作已取消"
            else:
                # 已审批 → 裸跑，让需要真实环境的脚本能跑通（沙箱内无网络/无缓存）
                obs = self._run(code, ctx, timeout, sandbox=False)
        elif not code:
            obs = "[run_python] code 不能为空"
        else:
            obs = self._run(code, ctx, timeout, sandbox=True)

        print(f"     [run_python] {code[:60].replace(chr(10), ' ')}")
        return ToolResult(
            tool_call_id=tc["id"], name="run_python",
            content=obs,
            step={"name": f"run_python_{tc['id']}", "type": "run_python"},
        )

    @staticmethod
    def _run(code: str, ctx: ToolContext, timeout: int, sandbox: bool) -> str:
        """写临时脚本并执行；无论如何都清理临时文件。"""
        tmp_dir = runtime_dir(ctx.base_dir) / "tmp"
        tmp_path = None
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / f"runpy_{os.getpid()}_{abs(hash(code)) % 10**8}.py"
            tmp_path.write_text(code, encoding="utf-8")
            exec_result = ctx.executor.run_step(
                f'python "{tmp_path}"', cwd=ctx.base_dir, timeout=timeout, sandbox=sandbox
            )
            return format_observation("run_python", exec_result, sandbox=exec_result.get("sandbox"))
        except Exception as e:
            return f"[run_python] 执行准备失败：{e}"
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
