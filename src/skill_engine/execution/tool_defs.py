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
def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read the contents of a file.

    Supports absolute paths, paths relative to the working directory,
    and ~/ expansion. Returns content with line numbers.
    Use offset/limit for pagination on large files.
    """  # noqa: E501


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file.

    Supports absolute paths, paths relative to the working directory,
    and ~/ expansion. Creates parent directories automatically.
    For new files only. To modify an existing file, use edit_file.
    """


@tool
def edit_file(path: str, edits: list[dict]) -> str:
    """Apply targeted edits to a file at `path`.

    Each edit replaces an exact `oldText` with `newText`.
    All edits are matched against the original file content, then applied
    in file order in one pass. Edits earlier in the file are applied first
    so later edits are not affected by newText of earlier edits.

    Args:
        path: Target file path (absolute, or relative to working directory).
        edits: List of {oldText, newText} dicts. Each `oldText` must appear
               exactly once in the file, or the entire operation fails.

    Returns:
        Success: "applied N edits to <path>"
        Failure: "error: <reason>"
    """


@tool
def search_files(pattern: str, path: str = ".", file_glob: str = "") -> str:
    """Search for a regex pattern in files within a directory.

    Uses Python's re module for pattern matching. Returns matching files
    with line numbers and context. This is a pure tool (no bash dependency),
    so it works even when bash is blocked.

    Args:
        pattern: Regex pattern to search for.
        path: Directory to search in (absolute, or relative to working directory).
        file_glob: Optional file filter (e.g. "*.py" to only search Python files).

    Returns:
        Matching lines with file paths and line numbers, or "no matches found".
    """


@tool
def stop(reason: str = "finished") -> str:
    """Signal that the task is complete and stop the tool dispatch loop.

    Call this when you have finished all work (reading, editing, verifying).
    Do NOT call this if you need to ask the user a question first.

    Args:
        reason: Brief description of what was accomplished.
    """


TOOL_DISPATCH_TOOLS = [bash, read_file, write_file, edit_file, search_files, stop]


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
    # ── 模式 1：整串是逗号分隔的 k=v 列表（值可含空格）──
    # 例：topic=two sum,difficulty=easy / topic=图论（graph theory 遍历）,difficulty=medium
    # 仅在 ASCII 逗号后紧跟新 key= 时才切分，值内的中文逗号/顿号不受影响。
    # 修复：旧正则 value 用 \S+ 贪婪匹配，"topic=array,difficulty=easy" 会把
    # ",difficulty=easy" 整段吞进 topic 的值，difficulty 丢失（下游 {difficulty}
    # 占位符不被替换）。
    segments = re.split(r',(?=\s*[^=\s,]+=)', query.strip())
    if len(segments) > 1:
        seg_pairs = []
        for seg in segments:
            m = re.match(r'^\s*([^=\s,]+)=(.*\S)\s*$', seg, re.S)
            if not m:
                seg_pairs = None
                break
            seg_pairs.append((m.group(1).strip(), m.group(2).rstrip(',;')))
        if seg_pairs:
            return dict(seg_pairs)
    # ── 模式 2：自然语言里散落的 k=v / k:v（原有行为）──
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