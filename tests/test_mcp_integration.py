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
