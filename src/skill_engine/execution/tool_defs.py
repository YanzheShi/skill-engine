"""档位 B 内建工具定义

工具通过 bind_tools 传给 LLM，让模型知道它可以调用哪些工具。
工具的实际执行由 ToolDispatchRunner 循环中的 executor / 文件操作完成。
"""

import re
from langchain_core.tools import tool


@tool
def bash(command: str) -> str:
    """Execute a shell command and return stdout.

    Note: On Windows the runtime is cmd.exe, not bash.
    - Use `python` (not python3) for scripts.
    - Do NOT use `mkdir -p`, just `mkdir`.
    - Paths with spaces must be quoted.
    - Do NOT use multi-line `python -c` commands.
    """  # noqa: E501


@tool
def read_file(path: str) -> str:
    """Read the contents of a file at the given path (relative to the skill directory)."""  # noqa: E501


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file at the given path (relative to the skill directory)."""


TOOL_DISPATCH_TOOLS = [bash, read_file, write_file]


def parse_named_params(query: str) -> dict:
    """从 query 中提取 named params（key=value 或 key:value 对）

    两种格式：
    - key=value（key 不限字符集，支持中文）：topic=DP 或 主题=DP
    - key:value（仅 ASCII 键，防中文冒号误伤）：topic:DP

    Args:
        query: 用户输入字符串

    Returns:
        {key: value, ...}，空 query 返回空 dict
    """
    if not query or not query.strip():
        return {}
    params = {}
    # key=value 格式，key 不限字符集
    for match in re.finditer(r'([^=\s]+)=(\S+)', query):
        key = match.group(1).strip()
        value = match.group(2).rstrip(',;')
        params[key] = value
    # key:value 格式，仅 ASCII 键（防中文冒号误伤）
    for match in re.finditer(r'([a-zA-Z]\w*):\s*(\S+)', query):
        key = match.group(1).lower()
        value = match.group(2).rstrip(',;')
        if key not in params:
            params[key] = value
    return params