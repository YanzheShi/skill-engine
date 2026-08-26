"""ask_user：session 模式专属——轮内暂停向用户提问并等待回答。

原来是 run() 内的 @tool 闭包（捕获 self.human_io），现升级为真正的
handler，靠 ctx 拿依赖。仅 session_mode 时注册。
"""

from langchain_core.tools import tool

from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


@tool
def ask_user(question: str = "") -> str:
    """在 session 持续会话中向用户提问并等待回答（轮内暂停）。

    当你需要用户的某个具体决策/确认才能继续当前任务时调用本工具，
    例如"选择方案 A 还是 B"。引擎会暂停并读取用户输入，把回答作为本
    工具的返回值回灌给你，你据此继续当前轮（不结束会话）。

    若只是在汇报进度或等待用户给出下一条指令，不要调用本工具，
    直接输出文本即可——引擎会在每轮结束后自动把控制权交还用户。
    """
    return ""  # schema-only：真正执行在 AskUserHandler


class AskUserHandler(BaseHandler):
    name = "ask_user"

    def schema(self):
        return ask_user

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        question = tc["input"].get("question", "")
        if ctx.human_io:
            if question:
                ctx.human_io.emit(question)
            content = ctx.human_io.read()
        else:
            content = ""
        content = content if isinstance(content, str) else str(content)
        if ctx.emit_tool:
            ctx.emit_tool("ask_user")
        if ctx.emit_result:
            trunc = ctx.truncate_msg(content, max_chars=800) if ctx.truncate_msg else content[:800]
            ctx.emit_result(trunc)
        return ToolResult(
            tool_call_id=tc["id"], name="ask_user",
            content=ctx.truncate_msg(content) if ctx.truncate_msg else content,
            step={
                "name": f"skill_tool_{tc['id']}",
                "type": "ask_user",
                "output": content[:1000],
            },
        )
