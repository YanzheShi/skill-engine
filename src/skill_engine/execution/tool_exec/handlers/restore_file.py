"""restore_file：通用文件检查点回滚（不依赖 git）。

原来是 run() 内的 @tool 闭包（捕获 base_dir / snapshot），现升级为真正的
handler，靠 ctx 拿依赖。引擎在每次修改文件前自动记录原始内容，本工具把
文件恢复到修改前状态。
"""

from pathlib import Path

from langchain_core.tools import tool

from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


@tool
def restore_file(path: str) -> str:
    """回滚文件到本次运行开始前的快照状态（通用文件检查点，不依赖 git）。

    引擎在每次修改文件前会自动记录其原始内容。出错时可调用本工具
    将该文件恢复到修改前的状态。

    Args:
        path: 要回滚的文件路径（绝对，或相对工作目录）
    """
    return ""  # schema-only：真正执行在 RestoreFileHandler


class RestoreFileHandler(BaseHandler):
    name = "restore_file"

    def schema(self):
        return restore_file

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        path = tc["input"].get("path", "")
        target = Path(path)
        if not target.is_absolute():
            target = (ctx.base_dir / path).resolve()
        else:
            target = target.resolve()
        ok, msg = ctx.snapshot.restore(target)
        content = msg if isinstance(msg, str) else str(msg)
        if ctx.emit_tool:
            ctx.emit_tool("restore_file")
        if ctx.emit_result:
            trunc = ctx.truncate_msg(content, max_chars=800) if ctx.truncate_msg else content[:800]
            ctx.emit_result(trunc)
        return ToolResult(
            tool_call_id=tc["id"], name="restore_file",
            content=ctx.truncate_msg(content) if ctx.truncate_msg else content,
            step={
                "name": f"skill_tool_{tc['id']}",
                "type": "restore_file",
                "output": content[:1000],
            },
        )
