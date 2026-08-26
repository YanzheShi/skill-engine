"""get_current_time：从 timeapi.io 获取当前时间（不信任本地时钟/训练知识）。"""

from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class GetCurrentTimeHandler(BaseHandler):
    name = "get_current_time"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        timezone = tc["input"].get("timezone", "Asia/Shanghai")
        try:
            import urllib.request
            import json as _json
            url = f"https://timeapi.io/api/Time/current/zone?timeZone={urllib.request.quote(timezone)}"
            req = urllib.request.Request(url, headers={"User-Agent": "skill-engine/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            obs = _json.dumps(data, ensure_ascii=False)
            return ToolResult(
                tool_call_id=tc["id"], name="get_current_time",
                content=ctx.truncate_msg(obs) if ctx.truncate_msg else obs,
                step={
                    "name": f"get_current_time_{tc['id']}",
                    "type": "get_current_time",
                    "timezone": timezone,
                    "output": obs[:1000],
                },
                print_line=f"     get_current_time {timezone}: {data.get('dateTime', '?')}",
            )
        except Exception as e:
            print(f"     get_current_time ERROR: {e}")
            return ToolResult(tool_call_id=tc["id"], name="get_current_time",
                              content=f"Get time failed: {e}")
