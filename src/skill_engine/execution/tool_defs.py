"""档位 B 内建工具定义

工具通过 bind_tools 传给 LLM，让模型知道它可以调用哪些工具。
工具的实际执行由 ToolDispatchRunner 循环中的 executor / 文件操作完成。
"""

import re
import importlib.util
import logging
from pathlib import Path
from langchain_core.tools import tool, BaseTool


@tool
def bash(command: str, timeout: int = 0) -> str:
    """Execute a shell command and return stdout.

    Note: On Windows the runtime is cmd.exe, not bash.
    - Use `python` (not python3) for scripts.
    - Do NOT use `mkdir -p`, just `mkdir`.
    - Paths with spaces must be quoted.
    - Do NOT use multi-line `python -c` commands.

    Args:
        timeout: Optional per-command timeout in seconds. 0 = engine default.
                 Raise it for long-running commands (test suites, builds),
                 e.g. timeout=300. The engine clamps it to a hard maximum.
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
        Rejected: "[用户拒绝了本次编辑] ..." when the skill enables edit
        confirmation and the user rejects the diff (file stays unchanged).
    """


@tool
def search_files(pattern: str, path: str = ".", file_glob: str = "", max_results: int = 0) -> str:
    """Search for a regex pattern in files within a directory.

    Uses ripgrep when available (fast, respects .gitignore), with a
    pure-Python fallback. Returns matching files with line numbers.
    This is a pure tool (no bash dependency), so it works even when
    bash is blocked.

    Args:
        pattern: Regex pattern to search for.
        path: Directory to search in (absolute, or relative to working directory).
        file_glob: Optional file filter (e.g. "*.py" to only search Python files).
        max_results: Optional result cap. 0 = default (100). If more matches
            exist, the output says so — narrow your pattern or path instead.

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


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using Tavily AI search and return structured JSON results.

    Tavily is designed for AI/LLM use — results include cleaned page content,
    relevance scores, and source URLs. Free tier: 1,000 searches/month.

    Set the TAVILY_API_KEY environment variable to use this tool.
    Get a free key at: https://app.tavily.com

    Use this when you need current information from the internet:
    - New library documentation, APIs, or version-specific features
    - Framework migration guides and best practices
    - Error solutions you haven't seen before
    - Current best practices, trends, or alternatives
    - Verifying assumptions about library behavior

    Args:
        query: The search query string.
        max_results: Maximum number of search results to return (1-10, default 5).

    Returns:
        JSON string: [{"title": "...", "url": "...", "content": "..."}, ...]
        or error message: "Search failed: ..."
    """


@tool
def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    """Get the current date and time from a web time API (not local clock).

    Uses timeapi.io to fetch the current time for a given timezone.
    Returns structured JSON with year, month, day, hour, minute, seconds,
    day of week, and whether DST is active.

    Args:
        timezone: IANA timezone name (e.g. "Asia/Shanghai", "America/New_York",
                  "Europe/London", "Asia/Tokyo"). Default is "Asia/Shanghai".

    Returns:
        JSON string with current time information, or error message.
    """


TOOL_DISPATCH_TOOLS = [bash, read_file, write_file, edit_file, search_files, stop, web_search, get_current_time]

# 工具注册表：将硬编码列表升级为可扩展注册表（通用引擎核心，不绑定领域语义）。
# 各 skill 可经 frontmatter 的 extra_tools 注入领域专属工具，核心不感知具体领域。
TOOL_REGISTRY: dict[str, BaseTool] = {t.name: t for t in TOOL_DISPATCH_TOOLS}


def load_skill_tools(skill) -> list[BaseTool]:
    """加载某个 skill 自带的领域工具（声明在 frontmatter 的 extra_tools 里）。

    约定：skill 目录下的 tools.py 等模块中，用 @tool 装饰器定义 BaseTool。
    引擎用 importlib 从绝对路径隔离加载，避免污染全局命名空间。
    返回的列表会与 TOOL_REGISTRY 合并后传给 bind_tools。

    Args:
        skill: Skill 对象（需有 .directory 与 .metadata.extra_tools）

    Returns:
        合并后的工具列表（空列表表示无额外工具）。
    """
    modules = getattr(skill.metadata, "extra_tools", None) or []
    if not modules:
        return []
    loaded: list[BaseTool] = []
    log = logging.getLogger("skill_engine.tool_defs")
    for rel in modules:
        tools_py = Path(skill.directory) / rel
        if not tools_py.exists():
            log.warning("extra_tools 声明的模块不存在: %s (skill=%s)", tools_py, skill.metadata.name)
            continue
        try:
            mod_name = "_skill_tools_" + re.sub(r"\W", "_", f"{skill.metadata.name}_{rel}")
            spec = importlib.util.spec_from_file_location(mod_name, str(tools_py))
            if spec is None or spec.loader is None:
                log.warning("无法为 %s 生成 import spec", tools_py)
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            log.warning("加载 skill 工具失败 %s: %s", tools_py, e)
            continue
        for v in vars(mod).values():
            if isinstance(v, BaseTool):
                # 命名冲突防护：同名工具后者覆盖（约定 skill 自带工具加领域前缀，如 cb_）
                loaded.append(v)
    return loaded


def load_mcp_tools(skill) -> list[BaseTool]:
    """加载 skill 经 mcp_servers 字段声明的 MCP 远程工具。

    复用 mcp_client.load_mcp_tools：从全局 mcp.json 解析 server 定义、连接，
    把远程工具拉成本地 BaseTool，与 extra_tools 一样合并进 bind_tools。
    无 mcp_servers 或连接失败都返回空列表（不中断执行）。

    Args:
        skill: Skill 对象（需有 .metadata.mcp_servers）

    Returns:
        合并后的工具列表（空列表表示无 MCP 工具）。
    """
    from skill_engine.execution.mcp_client import load_mcp_tools as _load_mcp
    server_names = getattr(skill.metadata, "mcp_servers", None) or []
    if not server_names:
        return []
    return _load_mcp(server_names)


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
