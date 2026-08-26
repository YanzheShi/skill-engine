"""write_file：整文件写入（安全门 + diff 预览 + 快照 + tracker）。"""

from skill_engine.execution.paths import resolve_path
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.edit_patch import _render_diff
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class WriteFileHandler(BaseHandler):
    name = "write_file"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        filepath = tc["input"].get("path", "")
        content = tc["input"].get("content", "")

        # 防御：LLM（尤其纯文本模型）可能漏传 path 键，导致 filepath 为空。
        # 空 path 会被解析成 base_dir 本身（目录），后续 write_text 在 Windows 上
        # 抛 PermissionError。这里提前拦截，回一条错误让模型补全 path 重试。
        if not filepath:
            print("     [SKIP] write_file 缺少 path 参数，已跳过")
            return ToolResult(
                tool_call_id=tc["id"], name="write_file",
                content=("[错误] write_file 缺少 path 参数，已跳过本次写入。"
                         "请补全要写入的文件路径（如 action_track/index.html）后重试。"),
            )

        # 安全门（只查路径，strict 不 BLOCK）
        approved, err_msg = ctx.check_file_safety("write", filepath, ctx.skill)
        if not approved:
            print(f"     {err_msg}")
            return ToolResult(tool_call_id=tc["id"], name="write_file", content=err_msg)

        full_path = resolve_path(filepath, ctx.base_dir)

        # diff 预览门（仅 confirm_edits 开启时进入）
        if ctx.confirm_edits_mode in ("true", "batch"):
            old_content = full_path.read_text(encoding="utf-8") if full_path.exists() else ""
            diff_text = _render_diff(filepath, old_content, content)
            if not ctx.confirm_edit("write_file", str(full_path), diff_text):
                print(f"     WRITE REJECTED by user: {filepath}")
                return ToolResult(
                    tool_call_id=tc["id"], name="write_file",
                    content="[用户拒绝了本次写入] 文件未变更。请调整方案或与用户澄清需求。",
                )

        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            # 写回前记录快照（仅已存在文件，避免回滚到"已删除"状态）
            if full_path.exists():
                ctx.snapshot.record(full_path, full_path.read_text(encoding="utf-8"))
            full_path.write_text(content, encoding="utf-8")
            ctx.file_tracker.on_write(full_path)
            return ToolResult(
                tool_call_id=tc["id"], name="write_file",
                content=f"wrote {len(content)} bytes to {filepath}",
                step={
                    "name": f"write_{tc['id']}",
                    "type": "write_file",
                    "path": str(full_path),
                },
                files_created=[str(full_path)],
                round_had_write=True,
                print_line=f"     wrote {len(content)} bytes to {filepath}",
            )
        except Exception as e:
            print(f"     ERROR: {e}")
            return ToolResult(tool_call_id=tc["id"], name="write_file",
                              content=f"[写入失败: {e}]")
