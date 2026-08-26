"""skill 自带工具 / MCP 工具的通用执行 handler（工具可插拔接口）。

bind_tools 只负责"让 LLM 知道有这些工具"，真正执行在此完成：
``tool_obj.invoke(input)``——与内建 handler 的 execute 同构。
"""

import os

from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class SkillToolHandler(BaseHandler):
    def __init__(self, tool_obj):
        self._tool_obj = tool_obj
        self.name = tool_obj.name

    def schema(self):
        return self._tool_obj

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        prev_cwd = os.getcwd()
        try:
            os.chdir(str(ctx.base_dir))  # 与 bash 一致的基准目录
            result = self._tool_obj.invoke(tc["input"])
        except Exception as e:
            result = f"[工具执行失败: {e}]"
        finally:
            os.chdir(prev_cwd)
        content = result if isinstance(result, str) else str(result)
        # 技能注入工具的真实输出走语义通道，展示实际内容（超长截断）而非仅字符数
        if ctx.emit_tool:
            ctx.emit_tool(tc["type"])
        if ctx.emit_result:
            display = ctx.truncate_msg(content, max_chars=800) if ctx.truncate_msg else content[:800]
            ctx.emit_result(display)
        return ToolResult(
            tool_call_id=tc["id"], name=tc["type"],
            content=ctx.truncate_msg(content) if ctx.truncate_msg else content,
            step={
                "name": f"skill_tool_{tc['id']}",
                "type": tc["type"],
                "output": content[:1000],
            },
        )
