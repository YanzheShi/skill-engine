"""shot_web：本机 Edge 无头模式网页截图。"""

from skill_engine.execution.tool_defs import _take_screenshot
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class ShotWebHandler(BaseHandler):
    name = "shot_web"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        url = tc["input"].get("url", "")
        if not url:
            print("     shot_web: empty url")
            return ToolResult(
                tool_call_id=tc["id"], name="shot_web",
                content="error: url 不能为空（支持 http(s):// 或本地文件路径）",
            )
        try:
            width = int(tc["input"].get("width", 1280))
            height = int(tc["input"].get("height", 800))
        except (TypeError, ValueError):
            width, height = 1280, 800
        full_page = bool(tc["input"].get("full_page", False))
        out = tc["input"].get("out", "screenshot.png")
        try:
            obs = _take_screenshot(
                url, width, height, full_page, out, str(ctx.base_dir))
            result = ToolResult(
                tool_call_id=tc["id"], name="shot_web",
                content=ctx.truncate_msg(obs) if ctx.truncate_msg else obs,
                step={
                    "name": f"shot_web_{tc['id']}",
                    "type": "shot_web",
                    "url": url,
                    "output": obs[:1000],
                },
            )
            if ctx.emit_tool:
                ctx.emit_tool("shot_web", url)
            if ctx.emit_result:
                trunc = ctx.truncate_msg(obs, max_chars=800) if ctx.truncate_msg else obs[:800]
                ctx.emit_result(trunc)
            return result
        except Exception as e:
            print(f"     shot_web ERROR: {e}")
            return ToolResult(tool_call_id=tc["id"], name="shot_web",
                              content=f"[截图失败: {e}]")
