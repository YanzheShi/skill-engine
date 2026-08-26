"""stop：任务完成信号，终止 agent loop。"""

from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class StopHandler(BaseHandler):
    name = "stop"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        # 原行为：直接返回，不追加任何 tool message
        return ToolResult(
            tool_call_id=tc["id"], name="stop",
            stop=True, stop_reason=tc["input"].get("reason", "stopped"),
        )
