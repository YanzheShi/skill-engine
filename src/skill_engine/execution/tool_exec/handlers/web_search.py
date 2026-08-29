"""web_search：Tavily AI 搜索（内建直连，默认/兜底实现）。

当有同名 MCP 工具（如 mcp-hub 网关）在线时会被其覆盖；本 handler 在无任何
MCP 提供 web_search、或 MCP 连接失败时作为兜底，直接用 TAVILY_API_KEY 调
Tavily，保证开源用户在未部署网关时仍有搜索能力。
"""

import json

from skill_engine.config import TAVILY_API_KEY
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class WebSearchHandler(BaseHandler):
    name = "web_search"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        query = tc["input"].get("query", "")
        max_results = int(tc["input"].get("max_results", 5))
        if not query:
            print("     web_search: empty query")
            return ToolResult(tool_call_id=tc["id"], name="web_search",
                              content="error: query 不能为空")
        api_key = TAVILY_API_KEY
        if not api_key:
            obs = "Search failed: TAVILY_API_KEY is not set. Get a free key at https://app.tavily.com"
            print(f"     web_search: {obs}")
            return ToolResult(tool_call_id=tc["id"], name="web_search", content=obs)
        try:
            from tavily import TavilyClient
            max_results = max(1, min(10, max_results))
            client = TavilyClient(api_key=api_key)
            result = client.search(query, max_results=max_results)
            results = result.get("results", [])
            obs = json.dumps(results, ensure_ascii=False)
            return ToolResult(
                tool_call_id=tc["id"], name="web_search",
                content=ctx.truncate_msg(obs) if ctx.truncate_msg else obs,
                step={
                    "name": f"web_search_{tc['id']}",
                    "type": "web_search",
                    "query": query,
                    "output": obs[:1000],
                },
                print_line=f"     web_search '{query}': {len(results)} results",
            )
        except Exception as e:
            print(f"     web_search ERROR: {e}")
            return ToolResult(tool_call_id=tc["id"], name="web_search",
                              content=f"Search failed: {e}")
