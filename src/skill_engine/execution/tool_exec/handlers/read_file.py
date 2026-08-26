"""read_file：文件读取（可并行批）。

三段式：主线程预处理（安全门 / 重复读检测 / 缓存）→ 工作线程纯磁盘读 →
主线程后处理（行号格式化 / tracker 登记 / 缓存写入）。
"""

from skill_engine.execution.paths import resolve_path
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BatchableHandler
from skill_engine.execution.tool_exec.read_util import _read_file_with_lines
from skill_engine.execution.tool_exec.result import ToolResult


class ReadFileHandler(BatchableHandler):
    name = "read_file"

    def prepare(self, tc: dict, ctx: ToolContext, step_results: list):
        inp = tc["input"]
        filepath = inp.get("path", "")
        approved, err_msg = ctx.check_file_safety("read", filepath, ctx.skill)
        if not approved:
            print(f"     {err_msg}")
            return ToolResult(tool_call_id=tc["id"], name="read_file", content=err_msg)
        full_path = resolve_path(filepath, ctx.base_dir)
        offset = int(inp.get("offset", 0))
        limit = int(inp.get("limit", 0))
        force_refresh = bool(inp.get("force_refresh", False))
        # 重复读取检测：对所有 read（含 force_refresh）累计同文件次数；仅当本次是
        # 「分页切片读」（给了 offset/limit，而非全文）且已达阈值时注入提示。
        # 全文读（无 offset/limit）不触发，避免干扰正常全文读。
        is_paged = (offset > 0 or limit > 0)
        read_count = sum(
            1 for s in step_results
            if s.get("type") == "read_file" and s.get("path") == str(full_path)
        )
        if read_count >= 3 and is_paged:
            # 绝不能在此追加一条独立 tool 消息——会与实际读取结果构成两个同 ID
            # tool 消息，触发 OpenAI duplicate tool call id 导致整轮 invoke 失败。
            # 把提示暂存到 tc，由 finish 阶段拼到读取结果内容开头（只产生一条消息）。
            # 阻断式重复读：第 4+ 次分页读且已有全文缓存时，不真正重读文件，
            # 直接复用已读过的全文缓存返回（不消耗迭代、不污染上下文）。
            full_hit = None
            if read_count >= 4 and ctx.file_tracker is not None:
                full_hit = ctx.file_tracker.cache_lookup(full_path, 0, 0)
            if full_hit is not None:
                tc["_force_cache_full"] = full_hit["content"]
                tc["_repeat_hint"] = (
                    f"[提示] 你已第 {read_count + 1} 次读取 {filepath}（含分页/force_refresh 切片读）。"
                    f"引擎已直接返回你**之前全文读取过的缓存内容**（见下方），请基于该内容工作，"
                    f"不要继续分页重试——反复切片读同一大文件会快速消耗迭代预算。\n\n"
                )
            else:
                tc["_repeat_hint"] = (
                    f"[提示] 你已第 {read_count + 1} 次读取 {filepath}（含 force_refresh 切片读）。"
                    f"反复分页/切片读取同一大文件会快速消耗迭代预算。请改用 "
                    f"force_refresh=true 且**不带 offset/limit** 一次性取全文，"
                    f"或若之前已读过则直接基于上文历史内容工作，不要继续分页重试。\n\n"
                )
            # 仍继续本次读取（不阻断），让模型拿到内容后收敛
        if not force_refresh and ctx.file_tracker is not None:
            hit = ctx.file_tracker.cache_lookup(full_path, offset, limit)
            if hit is not None:
                lo, hi = hit["start"], hit["end"]
                where = ("全文" if hit["full"] else f"第 {lo + 1}-{hi} 行")
                note = (
                    f"[read_file 缓存命中] {filepath} {where} 已在本会话早前"
                    f"读取过且文件未被修改，内容见上文历史。"
                    f"若上文内容已不可见，请带 force_refresh=true 重新调用以获取完整内容。"
                )
                if ctx.emit_tool:
                    ctx.emit_tool(f"read_file {filepath} (缓存命中 {where})")
                return ToolResult(
                    tool_call_id=tc["id"], name="read_file",
                    content=note,
                    step={
                        "name": f"read_{tc['id']}",
                        "type": "read_file",
                        "path": str(full_path),
                        "output": note[:1000],
                    },
                )
        tc["_rf"] = (full_path, offset, limit, filepath)
        return None

    def run_io(self, tc: dict, ctx: ToolContext):
        full_path = tc["_rf"][0]
        return ("ok", full_path.read_text(encoding="utf-8"))

    def finish(self, tc: dict, ctx: ToolContext, io_result: tuple) -> ToolResult:
        full_path, offset, limit, filepath = tc["_rf"]
        if io_result[0] == "err":
            print(f"     {io_result[1]}")
            return ToolResult(tool_call_id=tc["id"], name="read_file", content=io_result[1])
        content = io_result[1]
        # 若准备阶段标记了「直接复用全文缓存」（第4+次分页读且已有全文缓存），
        # 不实际读文件，用缓存全文满足本次读取（内容等价、不消耗迭代）。
        if tc.get("_force_cache_full") is not None:
            content = tc["_force_cache_full"]
        else:
            # 登记"已读版本"，供后续 edit 一致性校验
            ctx.file_tracker.on_read(full_path)
        formatted = _read_file_with_lines(content, offset, limit)
        if tc.get("_force_cache_full") is None:
            ctx.file_tracker.cache_read(
                full_path, offset, limit, len(content.splitlines()), formatted)
        if ctx.emit_tool:
            ctx.emit_tool(f"read_file {filepath}")
        if ctx.emit_result:
            trunc = ctx.truncate_msg(formatted, max_chars=800) if ctx.truncate_msg else formatted[:800]
            ctx.emit_result(trunc)
        msg = tc.get("_repeat_hint", "") + formatted
        return ToolResult(
            tool_call_id=tc["id"], name="read_file",
            content=ctx.truncate_msg(msg) if ctx.truncate_msg else msg,
            step={
                "name": f"read_{tc['id']}",
                "type": "read_file",
                "path": str(full_path),
                "output": formatted[:1000],
            },
        )
