"""search_files：正则搜索（可并行批，ripgrep 优先 / 纯 Python 回退）。"""

from skill_engine.execution.paths import resolve_path
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BatchableHandler
from skill_engine.execution.tool_exec.result import ToolResult
from skill_engine.execution.tool_exec.search import _search_files


class SearchFilesHandler(BatchableHandler):
    name = "search_files"

    def prepare(self, tc: dict, ctx: ToolContext, step_results: list):
        inp = tc["input"]
        pattern = inp.get("pattern", "")
        search_path = inp.get("path", ".")
        file_glob = inp.get("file_glob", "")
        if not pattern:
            return ToolResult(tool_call_id=tc["id"], name="search_files",
                              content="error: pattern 不能为空")
        search_dir = resolve_path(search_path, ctx.base_dir)
        if not search_dir.exists():
            print(f"     [路径不存在: {search_path}]")
            return ToolResult(tool_call_id=tc["id"], name="search_files",
                              content=f"[路径不存在: {search_path}]")
        tc["_sf"] = (pattern, search_dir, file_glob, int(inp.get("max_results", 0) or 0))
        return None

    def run_io(self, tc: dict, ctx: ToolContext):
        pattern, search_dir, file_glob, max_results_req = tc["_sf"]
        return ("ok", _search_files(pattern, search_dir, file_glob, max_results_req))

    def finish(self, tc: dict, ctx: ToolContext, io_result: tuple) -> ToolResult:
        pattern, search_dir, file_glob, max_results_req = tc["_sf"]
        if io_result[0] == "err":
            print(f"     {io_result[1]}")
            return ToolResult(tool_call_id=tc["id"], name="search_files", content=io_result[1])
        result = io_result[1]
        n_matches = 0 if result == "no matches found" else result.count("\n") + 1
        return ToolResult(
            tool_call_id=tc["id"], name="search_files",
            content=ctx.truncate_msg(result) if ctx.truncate_msg else result,
            step={
                "name": f"search_{tc['id']}",
                "type": "search_files",
                "pattern": pattern,
                "path": str(search_dir),
                "matches": n_matches,
            },
            print_line=f"     search '{pattern}' in {search_dir}: {n_matches} matches",
        )
