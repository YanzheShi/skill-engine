"""最小 MCP server（stdio），仅用于 skill-engine 的 MCP 集成测试。

用 mcp 1.x 的 FastMCP 暴露一个 echo_tool，验证引擎能连接 stdio MCP server
并把远程工具拉成本地 LangChain BaseTool、正常执行。
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mock-server")


@mcp.tool()
def echo_tool(message: str) -> str:
    """Echo the message back, prefixed with 'echo: '."""
    return f"echo: {message}"


@mcp.tool()
def add_tool(a: int, b: int) -> int:
    """Return the sum of two integers."""
    return a + b


if __name__ == "__main__":
    mcp.run(transport="stdio")
