"""档位 B 内建工具定义

工具通过 bind_tools 传给 LLM，让模型知道它可以调用哪些工具。
工具的实际执行由 ToolDispatchRunner 循环中的 executor / 文件操作完成。
"""

import re
import importlib.util
import logging
from pathlib import Path
from langchain_core.tools import tool, BaseTool

from skill_engine.execution.paths import runtime_dir


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
def read_file(path: str, offset: int = 0, limit: int = 0, force_refresh: bool = False) -> str:
    """Read the contents of a UTF-8 text file.

    Supports absolute paths, paths relative to the working directory,
    and ~/ expansion. Returns content with line numbers.
    Use offset/limit for pagination on large files.
    ONLY for text files — images and other binary files cannot be
    read this way; use view_image instead (vision models only).

    IMPORTANT: the engine caches read results per session. Re-reading a
    range you already read returns a cache-hit notice instead of the full
    text. If the earlier content is no longer visible in your context
    (e.g. it was compacted away), call again with force_refresh=true to
    always get the full content.
    """  # noqa: E501


@tool
def view_image(path: str) -> str:
    """Load an image file (PNG/JPEG/GIF/WebP) into the conversation for visual inspection.

    The image is injected as a multimodal message, so the model can SEE it.
    Before injection, when Pillow is available, it is downscaled (longest edge
    capped at 1568px) whenever it exceeds that cap — under area-linear image
    pricing (e.g. sensenova: ~1 token per 1024 px) any downscale strictly
    reduces tokens; the smaller of JPEG(q85)/PNG is sent. Images within the
    cap, or environments without Pillow, pass through unchanged.
    By default the image is injected inline as a base64 data URL — empirically
    SenseNova (sensenova-6.8-flash-lite) and the agnes gateway both accept
    base64 and *reject* public URLs (server-side egress blocked → HTTP400).
    R2 hosting is only used as an opt-in fallback when SKILL_ENGINE_USE_R2_IMAGES=1
    is set, for providers that genuinely require a public URL.
    ONLY works on models that support vision (vision: true in model profile).
    On text-only models it returns a notice instead of loading the image.
    Use this to check screenshots (e.g. from shot_web) and verify UI rendering.

    Args:
        path: Path to the image file (absolute, or relative to working directory).

    Returns:
        Confirmation that the image was loaded, or a notice that the current
        model cannot view images.
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
        edits: List of edit dicts. Two forms supported:
               - Plain: {"oldText", "newText"} — `oldText` must appear exactly
                 once in the whole file, or the whole operation fails.
               - Anchored: {"oldText", "newText", "line_range": [start, end]}
                 (1-indexed inclusive line range) — `oldText` is matched ONLY
                 within those lines, so it does NOT need to be globally unique.
                 Use this when `oldText` (e.g. a repeated assignment) appears
                 multiple times: give the line range you actually want to edit
                 instead of re-reading the file to find a unique snippet.

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
def update_plan(plan: str, status: str = "in_progress") -> str:
    """更新结构化任务清单（plan 模式——显式记录当前计划与进度）。

    引擎把每次调用记入执行轨迹（step_results），不污染 messages 历史、不参与上下文压缩、
    不消耗迭代预算，但续跑/压缩时可被引用，避免"忘了做到哪"。

    Args:
        plan: 当前计划（Markdown 清单，如 "- [ ] 定位根因\\n- [ ] 修复 database.py"）。
        status: 整体状态，in_progress / done / blocked。

    Returns:
        回显确认（实际存储由引擎内联完成，本函数体仅为 schema 声明）。
    """
    # 无 body：执行在 tool_dispatch 主循环内联实现（schema/执行分离，同 search_files）。
    return ""


@tool
def query_db(sql: str, db_path: str = "") -> str:
    """对工作目录内的 SQLite 数据库执行只读 SQL 查询，返回结果表格。

    用于快速验证数据（如统计 AC 率、查某表列名），避免为每次验证都 write_file 临时
    脚本 + bash 执行（那会浪费 2 轮迭代且污染工作目录）。仅允许 SELECT 等只读语句，
    DDL/DML 会被拒绝。

    Args:
        sql: 只读 SQL（SELECT / PRAGMA / EXPLAIN 等）。
        db_path: 数据库文件路径；留空则自动在工作目录下查找第一个 *.db 文件。

    Returns:
        查询结果（表格文本，经 format_observation 不截断）；或错误信息。
    """
    # 无 body：执行在 tool_dispatch 主循环内联实现（schema/执行分离，同 search_files）。
    return ""


@tool
def run_python(code: str, timeout: int = 30) -> str:
    """在工作目录的 Python 环境（引擎自动注入的 venv）中执行一段 Python 代码，返回 stdout/stderr。

    用于验证业务逻辑（如 import 项目模块、调用函数、检查返回值），替代
    `write_file 临时脚本 + bash python xxx.py` 或 `bash python -c "..."`（后者在
    cmd.exe 上多层引号易出错）。代码经 stdin 传给解释器，规避 shell 引号转义问题。

    注意：这是验证/探查工具，不是编辑器——不要用来做文件读写或长期脚本存放。

    Args:
        code: 要执行的 Python 代码（多行字符串）。
        timeout: 超时秒数（默认 30，防止死循环卡死）。

    Returns:
        执行结果（stdout/stderr，经 format_observation 不截断）；或错误信息。
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

    This is the built-in direct implementation. When an MCP server provides a
    tool with the same name (e.g. a shared mcp-hub gateway), that MCP tool
    takes precedence and this one is used only as a fallback.

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


@tool
def shot_web(url: str, width: int = 1280, height: int = 800,
             full_page: bool = False, out: str = "screenshot.png") -> str:
    """用本机 Edge 无头模式对网页截图，结果落盘到工作目录（任何 skill 均可调用）。

    底层调用系统已安装的 Microsoft Edge（Chromium 内核），无需额外下载浏览器。
    适合在开发网页时快速检视渲染效果——例如截 Vite/React 本地 dev server、
    或本地 HTML 文件。

    支持目标：
    - 在线地址：http:// 或 https:// 开头的 URL
    - 本地文件：传文件路径（自动转 file:/// 绝对路径），或显式 file:/// 形式

    Args:
        url: 要截图的地址（http(s):// 或本地文件路径）
        width: 视口宽度（像素），默认 1280
        height: 视口高度（像素），默认 800；full_page=True 时被忽略（截整页）
        full_page: True 截整页长图（需 websocket-client，缺失则降级为视口截图并提示）
        out: 输出文件名（相对工作目录或绝对路径），默认 screenshot.png

    Returns:
        落盘 PNG 的绝对路径字符串，或错误信息。
    """


def _find_edge() -> "str | None":
    """探测本机 Edge 可执行文件（Windows 优先），找不到返回 None。"""
    import shutil
    cand = shutil.which("msedge")
    if cand:
        return cand
    for p in (
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(p).exists():
            return p
    return None


def _shot_target(url: str, base_dir) -> "str | None":
    """把用户输入的 url 规整为 Edge 可识别的目标；本地文件转 file:///，不存在返回 None。"""
    if url.startswith(("http://", "https://", "file://")):
        return url
    p = Path(url)
    if not p.is_absolute():
        p = Path(base_dir) / p
    if p.exists():
        return "file:///" + str(p.resolve()).replace("\\", "/")
    return None


def _cdp_send(ws, method: str, params: "dict | None" = None) -> dict:
    """发送一条 CDP 命令并等待匹配 id 的响应（跳过中间的事件帧）。"""
    import json
    _cdp_send._id = getattr(_cdp_send, "_id", 0) + 1
    msg_id = _cdp_send._id
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        data = json.loads(ws.recv())
        if data.get("id") == msg_id:
            return data


def _take_fullpage_cdp(edge: str, target: str, width: int, out_abs: str, out_path: Path) -> str:
    """经 CDP（remote-debugging）测真实高度后截整页长图。需 websocket-client。"""
    import json
    import time
    import base64
    import tempfile
    import subprocess
    import urllib.request
    import websocket
    port = 9333
    user_data = tempfile.mkdtemp(prefix="edge_shot_")
    proc = subprocess.Popen(
        [edge, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
         f"--remote-debugging-port={port}", "--remote-allow-origins=*",
         f"--user-data-dir={user_data}", target],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        list_url = f"http://127.0.0.1:{port}/json"
        page_ws = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(list_url, timeout=1) as r:
                    targets = json.loads(r.read())
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        page_ws = t["webSocketDebuggerUrl"]
                        break
            except Exception:
                time.sleep(0.1)
            if page_ws:
                break
        if not page_ws:
            return "[截图失败] 无法连接 Edge 调试端口（full_page）"
        ws = websocket.create_connection(page_ws, timeout=10)
        try:
            _cdp_send(ws, "Page.enable")
            _cdp_send(ws, "Runtime.enable")
            time.sleep(3)  # 等待 JS 渲染（SPA/dev server 可能需要更久）
            h = _cdp_send(ws, "Runtime.evaluate",
                          {"expression": "document.documentElement.scrollHeight", "returnByValue": True})
            height = int((h.get("result", {}).get("result", {}).get("value", 0) or 0))
            if height <= 0:
                height = 800
            _cdp_send(ws, "Emulation.setDeviceMetricsOverride",
                      {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
            shot = _cdp_send(ws, "Page.captureScreenshot",
                             {"format": "png", "captureBeyondViewport": True})
            b64 = shot.get("result", {}).get("data", "")
            if not b64:
                return "[截图失败] CDP 截图数据为空"
            out_path.write_bytes(base64.b64decode(b64))
            return f"全页截图已保存: {out_abs} (宽 {width} x 高 {height})。如需视觉检查请用 view_image 工具读取该图片"
        finally:
            ws.close()
    except Exception as e:
        return f"[截图失败] full_page CDP 异常: {e}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _take_screenshot(url: str, width: int = 1280, height: int = 800,
                     full_page: bool = False, out: str = "screenshot.png",
                     base_dir=None) -> str:
    """用本机 Edge 无头模式截图（视口或全页），落盘并返回路径。

    视口截图零额外依赖；full_page 真全页走 CDP，需 websocket-client，
    缺失时自动降级为视口截图并给出提示。
    """
    import subprocess
    edge = _find_edge()
    if not edge:
        return "[截图失败] 未找到 Edge（msedge）。请安装 Microsoft Edge，或确认 msedge 在 PATH 中。"
    base_dir = Path(base_dir) if base_dir else Path.cwd()
    target = _shot_target(url, base_dir)
    if not target:
        return f"[截图失败] 目标不可识别或本地文件不存在: {url}"
    out_path = Path(out)
    if not out_path.is_absolute():
        # 相对路径（含默认 "screenshot.png"）统一收口到 <base_dir>/.skill-engine/screenshots/，
        # 避免截图污染工作目录；绝对路径（用户显式指定）则原样透传。
        out_path = runtime_dir(base_dir) / "screenshots" / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_abs = str(out_path.resolve())

    if not full_page:
        cmd = [edge, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
               f"--window-size={width},{height}", f"--screenshot={out_abs}", target]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as e:
            return f"[截图失败] {e}"
        if out_path.exists():
            return f"截图已保存: {out_abs} (视口 {width}x{height})。如需视觉检查请用 view_image 工具读取该图片"
        return f"[截图失败] Edge 未产出文件。stderr: {proc.stderr[:500]}"

    try:
        import websocket  # websocket-client
    except ImportError:
        cmd = [edge, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
               f"--window-size={width},{height}", f"--screenshot={out_abs}", target]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as e:
            return f"[截图失败] {e}"
        if out_path.exists():
            return (f"截图已保存(视口, 非全页): {out_abs}。如需视觉检查请用 view_image 工具读取该图片。\n"
                    f"[提示] full_page 真全页需要 websocket-client：运行 `uv add websocket-client` 后重试。")
        return f"[截图失败] Edge 未产出文件（full_page 降级路径）。"

    return _take_fullpage_cdp(edge, target, width, out_abs, out_path)


TOOL_DISPATCH_TOOLS = [bash, read_file, write_file, edit_file, search_files, stop, web_search, get_current_time, shot_web, view_image, update_plan, query_db, run_python]

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
    """加载 skill 可用的 MCP 远程工具。

    来源两部分（去重合并）：
    - skill 经 frontmatter 的 mcp_servers 字段声明的 server；
    - mcp.json 中标记 ``global: true`` 的 server（如 mcp-hub），对所有 skill
      常驻，使网关工具（如 web_search）成为引擎级能力而无需逐个声明。

    复用 mcp_client.load_mcp_tools：从全局 mcp.json 解析 server 定义、连接，
    把远程工具拉成本地 BaseTool，与 extra_tools 一样合并进 bind_tools。
    无可用 server 或连接失败都返回空列表（不中断执行）。

    Args:
        skill: Skill 对象（需有 .metadata.mcp_servers）

    Returns:
        合并后的工具列表（空列表表示无 MCP 工具）。
    """
    from skill_engine.execution.mcp_client import (
        load_mcp_tools as _load_mcp,
        discover_global_servers,
    )
    server_names = list(getattr(skill.metadata, "mcp_servers", None) or [])
    for name in discover_global_servers():
        if name not in server_names:
            server_names.append(name)
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
