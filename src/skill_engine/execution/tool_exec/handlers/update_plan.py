"""update_plan：结构化任务追踪。

仅记入 step_results，不污染 messages 历史、不参与压缩、不消耗迭代预算；
续跑/压缩时可被引用。
"""

from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class UpdatePlanHandler(BaseHandler):
    name = "update_plan"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        plan = tc["input"].get("plan", "")
        plan_status = tc["input"].get("status", "in_progress")
        return ToolResult(
            tool_call_id=tc["id"], name="update_plan",
            content=f"[计划已更新] status={plan_status}\n{plan}",
            print_line=f"     [计划更新] status={plan_status}",
            step={
                "name": f"plan_{tc['id']}",
                "type": "update_plan",
                "plan": plan,
                "status": plan_status,
            },
        )
