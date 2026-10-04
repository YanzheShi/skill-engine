"""MCP 支持集成测试（方案 A：全局 mcp.json + skill 字段引用 server 名）

覆盖：
- mcp_client._to_connection 对 stdio / http / sse 的归一化与非法配置容错
- mcp_client.load_mcp_tools 直连 mock stdio server，拿到工具并能 invoke 执行
- 未知 server 名 / 空列表的降级（返回 []，不抛异常）
- find_mcp_config 经环境变量定位
- tool_defs.load_mcp_tools(skill) 走完整路径（Skill.metadata.mcp_servers → 全局 mcp.json）
"""

import os
import sys
import json
import tempfile

import pytest

pytest.importorskip("langchain_mcp_adapters")
pytest.importorskip("mcp")

ASSET_DIR = __import__("pathlib").Path(__file__).parent / "assets"
MOCK_SERVER = ASSET_DIR / "mock_mcp_server.py"


def _make_config():
    """返回一个最小可用的 mcp.json mcpServers 配置（stdio，指向 mock server）。"""
    return {
        "mock": {
            "command": sys.executable,
            "args": [str(MOCK_SERVER)],
            "transport": "stdio",
        }
    }


# --------------------------- _to_connection 归一化 ---------------------------
def test_to_connection_stdio_workbench_style():
    from skill_engine.execution.mcp_client import _to_connection

    conn = _to_connection({"command": "uvx", "args": ["x", "serve"], "cwd": "/tmp", "type": "stdio"})
    assert conn["transport"] == "stdio"
    assert conn["command"] == "uvx"
    assert conn["args"] == ["x", "serve"]
    assert conn["cwd"] == "/tmp"


def test_to_connection_stdio_langchain_style():
    from skill_engine.execution.mcp_client import _to_connection

    conn = _to_connection({"transport": "stdio", "command": "node", "args": ["s.js"]})
    assert conn["transport"] == "stdio"
    assert conn["command"] == "node"


def test_to_connection_http_and_sse():
    from skill_engine.execution.mcp_client import _to_connection

    h = _to_connection({"url": "http://x/y", "type": "http"})
    assert h["transport"] == "streamable_http"
    assert h["url"] == "http://x/y"

    s = _to_connection({"transport": "sse", "url": "http://y/z", "headers": {"A": "1"}})
    assert s["transport"] == "sse"
    assert s["url"] == "http://y/z"
    assert s["headers"] == {"A": "1"}


def test_to_connection_invalid():
    from skill_engine.execution.mcp_client import _to_connection

    assert _to_connection({"type": "stdio"}) is None  # 缺 command
    assert _to_connection({"type": "weird"}) is None  # 未知 transport
    assert _to_connection({}) is None


# --------------------------- load_mcp_tools 主路径 ---------------------------
def test_load_mcp_tools_from_config():
    from skill_engine.execution.mcp_client import load_mcp_tools

    tools = load_mcp_tools(["mock"], config=_make_config())
    assert tools, "应至少加载到一个 MCP 工具"
    names = {t.name for t in tools}
    assert "echo_tool" in names
    assert "add_tool" in names

    echo = next(t for t in tools if t.name == "echo_tool")
    result = echo.invoke({"message": "hello"})
    assert "echo: hello" in str(result)

    add = next(t for t in tools if t.name == "add_tool")
    assert "3" in str(add.invoke({"a": 1, "b": 2}))


def test_load_mcp_tools_unknown_server_is_empty():
    from skill_engine.execution.mcp_client import load_mcp_tools

    assert load_mcp_tools(["nope"], config=_make_config()) == []


def test_load_mcp_tools_empty_input():
    from skill_engine.execution.mcp_client import load_mcp_tools

    assert load_mcp_tools([]) == []
    assert load_mcp_tools(None) == []  # type: ignore[arg-type]


# --------------------------- 配置发现与完整 skill 路径 ---------------------------
def test_find_mcp_config_via_env(tmp_path, monkeypatch):
    from skill_engine.execution.mcp_client import find_mcp_config

    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": _make_config()}))
    monkeypatch.setenv("SKILL_ENGINE_MCP_CONFIG", str(p))
    assert find_mcp_config() == p


def test_tool_defs_load_mcp_tools_via_skill(tmp_path, monkeypatch):
    from skill_engine.models import Skill, SkillMetadata
    from skill_engine.execution.tool_defs import load_mcp_tools as load_mcp_tools_for_skill

    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": _make_config()}))
    monkeypatch.setenv("SKILL_ENGINE_MCP_CONFIG", str(p))

    skill = Skill(
        metadata=SkillMetadata(name="demo", description="d", mcp_servers=["mock"]),
        body="",
        directory=str(tmp_path),
    )
    tools = load_mcp_tools_for_skill(skill)
    assert any(t.name == "echo_tool" for t in tools)


def test_run_merges_mcp_tools_into_bind_tools(tmp_path, monkeypatch):
    """端到端：skill.metadata.mcp_servers 经 run() 真实并入 bind_tools。"""
    from unittest.mock import MagicMock
    from langchain_core.messages import AIMessage
    from skill_engine.models import Skill, SkillMetadata, MatchResult
    from skill_engine.execution.tool_dispatch import ToolDispatchRunner

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": _make_config()}))
    monkeypatch.setenv("SKILL_ENGINE_MCP_CONFIG", str(cfg))

    skill = Skill(
        metadata=SkillMetadata(name="demo", description="d", mcp_servers=["mock"]),
        body="",
        directory=str(tmp_path),
    )
    match = MatchResult(skill=skill, score=1.0, method="name", arguments={})

    captured = {}
    llm = MagicMock()

    def _bind(tools):
        captured["tools"] = tools
        return llm

    llm.bind_tools.side_effect = _bind
    llm.invoke.return_value = AIMessage(content="done")

    runner = ToolDispatchRunner(
        executor=MagicMock(), assembler=MagicMock(), working_root=str(tmp_path)
    )
    runner.run(match, llm, max_iterations=2)

    names = {t.name for t in captured["tools"]}
    assert "echo_tool" in names, f"MCP 工具未并入 bind_tools: {names}"
    assert "bash" in names and "read_file" in names, "内建工具应仍在"


# --------------------------- 连接超时（防半死 server 忙等） ---------------------------
# 背景（2026-10-04）：load_mcp_tools 曾对 get_tools() 裸跑 asyncio.run，无任何超时。
# server 端口没人听会快速失败（refused），但「端口通、却不响应 MCP 握手」的半死
# server（假死 hub / 代理黑洞）会把 asyncio.run **永久挂起**，连 warning 都打不出。
# 修复后统一走 asyncio.wait_for(MCP_CONNECT_TIMEOUT_S)。

import socket as _socket
import threading as _threading
import time as _time


def _accept_loop(srv, n=5):
    """accept n 个连接后静默退出；teardown 关 socket 时容错，不炸线程。"""
    for _ in range(n):
        try:
            srv.accept()
        except OSError:
            return


@pytest.fixture
def dead_http_server():
    """起一个只 listen+accept、永不回 MCP 握手的假 server，模拟半死 hub。"""
    srv = _socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    t = _threading.Thread(target=_accept_loop, args=(srv,), daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/mcp"
    srv.close()


def test_load_mcp_tools_refused_server_is_fast():
    """server 端口没人听：快速失败降级，返回 [] 且不抛异常。"""
    from skill_engine.execution.mcp_client import load_mcp_tools

    t0 = _time.monotonic()
    tools = load_mcp_tools(
        ["dead"],
        config={"dead": {"url": "http://127.0.0.1:1/mcp", "transport": "streamable_http"}},
    )
    assert tools == []
    assert _time.monotonic() - t0 < 10, "connection refused 应秒级降级，不应长时间阻塞"


def test_load_mcp_tools_dead_server_times_out(monkeypatch, dead_http_server):
    """半死 server：30s 兜底超时生效，不永久挂起（改前此用例会挂死整个测试）。"""
    from skill_engine.execution import mcp_client
    from skill_engine.execution.mcp_client import load_mcp_tools

    monkeypatch.setattr(mcp_client, "MCP_CONNECT_TIMEOUT_S", 1)
    t0 = _time.monotonic()
    tools = load_mcp_tools(
        ["dead"],
        config={"dead": {"url": dead_http_server, "transport": "streamable_http"}},
    )
    elapsed = _time.monotonic() - t0
    assert tools == []
    assert elapsed < 15, f"1s 超时应很快返回，实际 {elapsed:.1f}s"


def test_load_mcp_tools_timeout_is_isolated(monkeypatch, dead_http_server):
    """单点失败隔离：同批 server 里假死的那个超时降级，不影响其他 server 加载。"""
    from skill_engine.execution import mcp_client
    from skill_engine.execution.mcp_client import load_mcp_tools

    # 注意：不能给 1s——stdio mock 冷启动（spawn 子进程 + JSON-RPC 握手）
    # 在 Windows 上就要 >1s，会被误掐；8s 对假死 server 也够快。
    monkeypatch.setattr(mcp_client, "MCP_CONNECT_TIMEOUT_S", 8)
    config = {
        "dead": {"url": dead_http_server, "transport": "streamable_http"},
        "mock": {"command": sys.executable, "args": [str(MOCK_SERVER)], "transport": "stdio"},
    }
    tools = load_mcp_tools(["dead", "mock"], config=config)
    assert tools, "假死 server 超时不应影响 mock stdio server 的工具加载"
    assert all(t.name for t in tools)


def test_load_mcp_tools_timeout_warns(monkeypatch, dead_http_server, caplog):
    """超时降级必须打 warning（观测口径：silent hang 不可接受）。"""
    import logging
    from skill_engine.execution import mcp_client
    from skill_engine.execution.mcp_client import load_mcp_tools

    monkeypatch.setattr(mcp_client, "MCP_CONNECT_TIMEOUT_S", 1)
    with caplog.at_level(logging.WARNING, logger="skill_engine.mcp_client"):
        tools = load_mcp_tools(
            ["dead"],
            config={"dead": {"url": dead_http_server, "transport": "streamable_http"}},
        )
    assert tools == []
    assert any("dead" in r.getMessage() for r in caplog.records), \
        "超时降级应打出含 server 名的 warning"
