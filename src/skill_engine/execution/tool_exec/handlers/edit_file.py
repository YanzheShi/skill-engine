"""edit_file：定点编辑（安全门 + read-before-write 校验 + 模糊匹配 + diff 预览）。"""

from skill_engine.execution.paths import resolve_path
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.edit_patch import _apply_edits, _render_diff
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class EditFileHandler(BaseHandler):
    name = "edit_file"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        filepath = tc["input"].get("path", "")
        edits = tc["input"].get("edits", [])

        if not edits:
            return ToolResult(tool_call_id=tc["id"], name="edit_file",
                              content="error: edits 列表为空")

        # 安全门（只查路径，strict 不 BLOCK）
        approved, err_msg = ctx.check_file_safety("edit", filepath, ctx.skill)
        if not approved:
            print(f"     {err_msg}")
            return ToolResult(tool_call_id=tc["id"], name="edit_file", content=err_msg)

        full_path = resolve_path(filepath, ctx.base_dir)

        if not full_path.exists():
            print(f"     FILE NOT FOUND: {filepath}")
            return ToolResult(
                tool_call_id=tc["id"], name="edit_file",
                content=f"error: 文件不存在: {filepath}（新文件请用 write_file）",
            )

        # 编辑前一致性校验。软约束（默认）注入提示不阻断；硬约束
        # （strict_file_tracking）拒绝执行，引导 LLM 重读后重试。
        track_ok, track_msg = ctx.file_tracker.check_editable(full_path)
        if not track_ok:
            print(f"     EDIT BLOCKED (file tracker): {filepath}")
            return ToolResult(tool_call_id=tc["id"], name="edit_file", content=track_msg)

        try:
            content = full_path.read_text(encoding="utf-8")

            # 校验 + 应用：精确优先，oldText 不存在时走行级宽松模糊匹配
            new_content, err = _apply_edits(content, edits)
            if err:
                print(f"     EDIT FAILED: {err[:80]}")
                return ToolResult(
                    tool_call_id=tc["id"], name="edit_file",
                    content=err + (f"\nhint: {track_msg}" if track_msg else ""),
                )

            # diff 预览门（仅 confirm_edits 开启时进入）
            if ctx.confirm_edits_mode in ("true", "batch"):
                diff_text = _render_diff(filepath, content, new_content)
                if not ctx.confirm_edit("edit_file", str(full_path), diff_text):
                    print(f"     EDIT REJECTED by user: {filepath}")
                    return ToolResult(
                        tool_call_id=tc["id"], name="edit_file",
                        content="[用户拒绝了本次编辑] 文件未变更。请调整编辑方案或与用户澄清需求。",
                    )

            # 写回前记录快照（通用文件检查点，仅首次记录进入前状态）
            ctx.snapshot.record(full_path, content)

            full_path.write_text(new_content, encoding="utf-8")
            ctx.file_tracker.on_write(full_path)

            result_msg = f"applied {len(edits)} edits to {filepath}"
            if track_msg:
                result_msg += f"\n{track_msg}"
            return ToolResult(
                tool_call_id=tc["id"], name="edit_file",
                content=result_msg,
                step={
                    "name": f"edit_{tc['id']}",
                    "type": "edit_file",
                    "path": str(full_path),
                    "edits_count": len(edits),
                },
                files_created=[str(full_path)],
                round_had_write=True,
                print_line=f"     {result_msg}",
            )
        except Exception as e:
            print(f"     ERROR: {e}")
            return ToolResult(tool_call_id=tc["id"], name="edit_file",
                              content=f"[编辑失败: {e}]")
