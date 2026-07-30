"""MCP 连接层（方案 A：全局 mcp.json + skill 字段引用 server 名）

职责：
- 从全局 mcp.json（WorkBuddy 同款格式）读取 server 定义
- 经 langchain-mcp-adapters 连接 stdio / streamable_http / sse 类型的 MCP server
- 把远程工具拉成本地 LangChain BaseTool，交给通用工具执行分支（tool_dispatch 的
  skill_tools_map）统一调度——复用 P1 的 extra_tools 管线，核心零改动

设计要点：
- 采用"每调用重连"模式：load_mcp_tools 一次性拿到工具对象（每个工具自带 session
  manager），之后 tool.invoke() 内部自动连接/断开。无需在 run() 的 finally 里挂生命周期。
- 单个 server 连接失败不影响其他 server，也不会中断整个 skill 执行（仅告警）。
- langchain_mcp_adapters 包仅在真正用到 MCP 时才 import（lazy），不影响无 MCP 的普通路径。
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional, Any

logger = logging.getLogger("skill_engine.mcp_client")

from langchain_core.tools import BaseTool


# ---------------------------------------------------------------------------
# 配置发现与解析
# ---------------------------------------------------------------------------
def find_mcp_config() -> Optional[Path]:
    """定位全局 mcp.json。

    优先级：
    1. 环境变量 SKILL_ENGINE_MCP_CONFIG 显式指定
    2. ~/.workbuddy/mcp.json（WorkBuddy 默认位置）
    3. <当前工作目录>/.workbuddy/mcp.json

    Returns:
        找到返回 Path，否则 None。
    """
    env = os.environ.get("SKILL_ENGINE_MCP_CONFIG")
    if env:
        p = Path(env)
        return p if p.exists() else None
    candidates = [
        Path.home() / ".workbuddy" / "mcp.json",
        Path.cwd() / ".workbuddy" / "mcp.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def load_mcp_config(path: Path) -> dict:
    """读取 mcp.json，返回 mcpServers 字典（server 名 → 连接参数）。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("mcpServers", {})


def _to_connection(entry: dict) -> Optional[dict]:
    """把 mcp.json 里的一个 server 定义归一化为 langchain-mcp-adapters 的 connection 字典。

    兼容两种字段命名：
    - transport: "stdio" | "streamable_http" | "sse"（langchain 原生）
    - type: "stdio" | "http" | "sse"（WorkBuddy mcp.json 风格）

    Returns:
        归一化后的 connection dict；参数缺失或 transport 未知则返回 None。
    """
    transport = entry.get("transport") or entry.get("type")
    if transport in (None, "", "stdio", "stdin"):
        command = entry.get("command")
        if not command:
            logger.warning("_to_connection: stdio server 缺少 command，跳过")
            return None
        conn: dict = {"transport": "stdio", "command": command}
        if entry.get("args"):
            conn["args"] = entry["args"]
        if entry.get("env"):
            conn["env"] = entry["env"]
        if entry.get("cwd"):
            conn["cwd"] = entry["cwd"]
        return conn
    if transport in ("streamable_http", "streamable-http", "http", "http+sse"):
        url = entry.get("url")
        if not url:
            logger.warning("_to_connection: http server 缺少 url，跳过")
            return None
        conn = {"transport": "streamable_http", "url": url}
        if entry.get("headers"):
            conn["headers"] = entry["headers"]
        return conn
    if transport == "sse":
        url = entry.get("url")
        if not url:
            logger.warning("_to_connection: sse server 缺少 url，跳过")
            return None
        conn = {"transport": "sse", "url": url}
        if entry.get("headers"):
            conn["headers"] = entry["headers"]
        return conn
    logger.warning("_to_connection: 未知 transport=%r，跳过", transport)
    return None


# ---------------------------------------------------------------------------
# 异步桥接：在同步的 run() 里调用 async 的 get_tools()
# ---------------------------------------------------------------------------
def _run_async(coro):
    """在同步上下文中运行协程。

    - 无活跃事件循环时直接用 asyncio.run。
    - 已有活跃循环（如 gradio 托管）时，另起线程跑，避免 "cannot run nested event loop"。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


# ---------------------------------------------------------------------------
# async-only → sync 包装：让 tool_dispatch 的通用 .invoke 分支可直接调用 MCP 工具
# ---------------------------------------------------------------------------
class _SyncMCPTool(BaseTool):
    """把 async-only 的 MCPTool 包装成支持同步 invoke 的 BaseTool。

    langchain-mcp-adapters 生成的工具是 async-only（_run 抛 NotImplementedError）。
    包装后对外暴露同步 _run，内部用 asyncio.run 驱动底层 MCP 工具的 ainvoke，
    从而 tool_dispatch 的通用执行分支（tool_obj.invoke(...)）无需任何改动即可调用。
    """

    mcp_tool: Any = None

    def _run(self, **kwargs) -> Any:
        return asyncio.run(self.mcp_tool.ainvoke(kwargs))

    async def _arun(self, **kwargs) -> Any:
        return await self.mcp_tool.ainvoke(kwargs)


def _wrap_mcp_tool(tool) -> BaseTool:
    """包装单个 MCPTool：保留 name/description/args_schema，转为同步可调用。"""
    return _SyncMCPTool(
        name=tool.name,
        description=tool.description or "",
        args_schema=tool.args_schema,
        mcp_tool=tool,
    )


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def load_mcp_tools(server_names: list[str], config: Optional[dict] = None) -> list:
    """连接一组 MCP server，返回拉取到的 LangChain 工具列表。

    Args:
        server_names: skill 的 mcp_servers 字段（引用 mcp.json 的 server 名）
        config: 可选的 server 定义字典；不传则自动从全局 mcp.json 发现

    Returns:
        合并后的 BaseTool 列表（空列表表示无可用 MCP 工具）。
    """
    if not server_names:
        return []
    if config is None:
        path = find_mcp_config()
        if path is None:
            logger.warning("未找到 mcp.json，MCP 工具不可用（mcp_servers=%s）", server_names)
            return []
        config = load_mcp_config(path)

    # lazy import：仅在真正用到 MCP 时才引入 langchain_mcp_adapters，避免拖慢普通路径
    from langchain_mcp_adapters.client import MultiServerMCPClient

    tools: list = []
    for name in server_names:
        entry = config.get(name)
        if not entry:
            logger.warning("mcp_servers 引用的 server 未在 mcp.json 找到: %s", name)
            continue
        # 尊重 WorkBuddy mcp.json 的 disabled 语义：显式禁用则不连接
        if entry.get("disabled"):
            logger.info("MCP server '%s' 在 mcp.json 中标记为 disabled，跳过", name)
            continue
        conn = _to_connection(entry)
        if conn is None:
            continue
        try:
            # 逐个 server 连接：单点失败隔离，不影响其他 server 与后续执行
            client = MultiServerMCPClient({name: conn})
            server_tools = _run_async(client.get_tools())
            if server_tools:
                # 包装为同步可调用工具：MCP 工具原生 async-only，经 _SyncMCPTool
                # 包装后，tool_dispatch 的通用 .invoke 分支即可直接调用（核心零改动）。
                wrapped = [_wrap_mcp_tool(t) for t in server_tools]
                tools.extend(wrapped)
                logger.info("已加载 MCP server '%s' 的 %d 个工具", name, len(wrapped))
        except Exception as e:  # 连接/握手异常，降级为告警而非中断
            logger.warning("连接 MCP server 失败 '%s': %s", name, e)
    return tools
