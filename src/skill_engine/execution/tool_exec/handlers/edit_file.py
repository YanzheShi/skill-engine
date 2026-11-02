"""edit_file：定点编辑（安全门 + read-before-write 校验 + 模糊匹配 + diff 预览）。

若目标存在未转正的 ``<name>.partial``（上次写入被截断/尚未填完），
则优先编辑该 partial；编辑后完整度通过才原子转正为正式文件。
"""

from skill_engine.execution.paths import resolve_path
from skill_engine.execution.tool_exec import atomic_write
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.edit_patch import _apply_edits, _render_diff
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.handlers.write_file import _completeness_note
from skill_engine.execution.tool_exec.result import CALL_TRUNCATED_HINT, ToolResult


class EditFileHandler(BaseHandler):
    name = "edit_file"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        filepath = tc["input"].get("path", "")
        edits = tc["input"].get("edits", [])

        # 必须先判空：空 path 会被 resolve_path 解析成 base_dir（目录），
        # 随后安全门返回「[拒绝] 目标路径是目录」——模型据此完全无法修正
        # （实测唯一一次 edit_file 调用就死在这句话上，此后全程改用整文件重写）。
        if not filepath:
            print("     [SKIP] edit_file 缺少 path 参数，已跳过")
            return ToolResult(
                tool_call_id=tc["id"], name="edit_file",
                content=CALL_TRUNCATED_HINT.format(missing="path"),
            )

        if not edits:
            return ToolResult(tool_call_id=tc["id"], name="edit_file",
                              content="error: edits 列表为空")

        # 安全门（只查路径，strict 不 BLOCK）
        approved, err_msg = ctx.check_file_safety("edit", filepath, ctx.skill)
        if not approved:
            print(f"     {err_msg}")
            return ToolResult(tool_call_id=tc["id"], name="edit_file", content=err_msg)

        full_path = resolve_path(filepath, ctx.base_dir)
        ppath = atomic_write.partial_path(full_path)
        staging = atomic_write.enabled()
        # 优先编辑未转正的 partial；否则编辑正式文件
        work_path = ppath if (staging and ppath.exists()) else full_path

        if not work_path.exists():
            print(f"     FILE NOT FOUND: {filepath}")
            return ToolResult(
                tool_call_id=tc["id"], name="edit_file",
                content=f"error: 文件不存在: {filepath}（新文件请用 write_file）",
            )

        # 编辑前一致性校验。partial 是本引擎暂存的半成品，不参与"已读"校验。
        if work_path == full_path:
            track_ok, track_msg = ctx.file_tracker.check_editable(full_path)
            if not track_ok:
                print(f"     EDIT BLOCKED (file tracker): {filepath}")
                return ToolResult(tool_call_id=tc["id"], name="edit_file", content=track_msg)
        else:
            track_msg = ""

        try:
            content = work_path.read_text(encoding="utf-8")

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
            ctx.snapshot.record(work_path, content)
            work_path.write_text(new_content, encoding="utf-8")

            finalized = True
            verdict = ""
            if work_path == ppath:
                verdict = _completeness_note(full_path, new_content)
                finalized = atomic_write.should_finalize(
                    verdict, bool(getattr(ctx, "turn_truncated", False)))
                if finalized:
                    if full_path.exists():
                        ctx.snapshot.record(full_path, full_path.read_text(encoding="utf-8"))
                    atomic_write.finalize(ppath, full_path)
                    ctx.file_tracker.on_write(full_path)
                else:
                    ctx.file_tracker.on_write(ppath)
            else:
                ctx.file_tracker.on_write(full_path)

            if finalized:
                result_msg = f"applied {len(edits)} edits to {filepath}"
            else:
                first = verdict.splitlines()[0] if verdict else "complete: NO"
                result_msg = (f"applied {len(edits)} edits to {filepath}.partial; "
                              f"target NOT written → {first}")
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
                    "staged": not finalized,
                },
                files_created=[str(full_path)] if finalized else [],
                round_had_write=finalized,
                print_line=f"     {result_msg}",
            )
        except Exception as e:
            print(f"     ERROR: {e}")
            return ToolResult(tool_call_id=tc["id"], name="edit_file",
                              content=f"[编辑失败: {e}]")
