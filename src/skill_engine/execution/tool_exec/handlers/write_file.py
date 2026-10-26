"""write_file：整文件写入 / 追加（安全门 + diff 预览 + 快照 + tracker）。"""

import json
from pathlib import Path

from skill_engine.execution.paths import resolve_path
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.edit_patch import _render_diff
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import CALL_TRUNCATED_HINT, ToolResult

_TAIL_PREVIEW_CHARS = 60


def _completeness_note(path: Path, text: str) -> str:
    """给模型的完整度回执（只对能低成本判定的类型下结论）。

    这条回执替代模型自己写 ``python -c "print(len(c), c.endswith('</html>'))"``
    来检查文件的那一幕——实测这样的自检做了 6 次，每次一整轮 LLM 往返。
    """
    suffix = path.suffix.lower()
    if suffix in (".html", ".htm"):
        if "</html>" in text[-400:]:
            return "complete: yes (ends with </html>)"
        return ('complete: NO (missing </html> — 文件很可能在生成途中被截断)\n'
                '请用 write_file(..., mode="append") 续写剩余部分，不要重发整个文件')
    if suffix == ".json" and len(text) <= 512 * 1024:
        try:
            json.loads(text)
            return "complete: yes (valid JSON)"
        except Exception as e:  # noqa: BLE001 — 只是回执，不阻断
            return f"complete: NO (invalid JSON: {e.__class__.__name__})"
    return "complete: unknown (该类型未做闭合校验)"


class WriteFileHandler(BaseHandler):
    name = "write_file"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        filepath = tc["input"].get("path", "")
        content = tc["input"].get("content", "")
        mode = str(tc["input"].get("mode") or "overwrite").strip().lower()

        # 空 path 会被解析成 base_dir（目录），在 Windows 上 write_text 抛
        # PermissionError；而缺 path 的真因多半是输出被截断，报错必须点明这一点。
        if not filepath:
            print("     [SKIP] write_file 缺少 path 参数，已跳过")
            return ToolResult(
                tool_call_id=tc["id"], name="write_file",
                content=CALL_TRUNCATED_HINT.format(missing="path"),
            )
        if mode not in ("overwrite", "append"):
            return ToolResult(
                tool_call_id=tc["id"], name="write_file",
                content=f"[参数错误] mode 只支持 overwrite / append，收到 {mode!r}",
            )

        # 安全门（只查路径，strict 不 BLOCK）
        approved, err_msg = ctx.check_file_safety("write", filepath, ctx.skill)
        if not approved:
            print(f"     {err_msg}")
            return ToolResult(tool_call_id=tc["id"], name="write_file", content=err_msg)

        full_path = resolve_path(filepath, ctx.base_dir)
        existed = full_path.exists()
        old_content = full_path.read_text(encoding="utf-8") if existed else ""

        if mode == "append" and existed:
            final_content = old_content
            if final_content and not final_content.endswith("\n") and content \
                    and not content.startswith("\n"):
                final_content += "\n"   # 断点续写：避免把上一行接成一行
            final_content += content
        else:
            if mode == "append":
                mode = "overwrite"      # 追加目标不存在 = 新建，语义退化为覆盖
            final_content = content

        # diff 预览门（仅 confirm_edits 开启时进入）
        if ctx.confirm_edits_mode in ("true", "batch"):
            diff_text = _render_diff(filepath, old_content, final_content)
            if not ctx.confirm_edit("write_file", str(full_path), diff_text):
                print(f"     WRITE REJECTED by user: {filepath}")
                return ToolResult(
                    tool_call_id=tc["id"], name="write_file",
                    content="[用户拒绝了本次写入] 文件未变更。请调整方案或与用户澄清需求。",
                )

        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            # 写回前记录快照（仅已存在文件，避免回滚到"已删除"状态）
            if existed:
                ctx.snapshot.record(full_path, old_content)
            full_path.write_text(final_content, encoding="utf-8")
            ctx.file_tracker.on_write(full_path)

            total = len(final_content)
            lines = final_content.count("\n") + (
                0 if not final_content or final_content.endswith("\n") else 1)
            tail = final_content[-_TAIL_PREVIEW_CHARS:].replace("\n", "\\n")
            verdict = _completeness_note(full_path, final_content)
            receipt = (
                f"wrote {len(content)} bytes to {filepath} (mode={mode}); "
                f"file is now {total} bytes / {lines} lines\n"
                f"tail: ...{tail}\n"
                + verdict
            )
            summary = (f"wrote {len(content)} bytes → {filepath} ({mode}); "
                       f"{total} bytes / {lines} lines · {verdict.splitlines()[0]}")
            emit = getattr(ctx, "emit_result", None)
            if emit is not None:
                # 走 emit_result 而不是 print_line：只有 emit 通道会落 trace 事件，
                # 否则这次运行里最关键的健康信号（complete: NO）在日志中完全不可见。
                emit(summary)
                print_line = ""
            else:
                print_line = f"     {summary}"
            return ToolResult(
                tool_call_id=tc["id"], name="write_file",
                content=receipt,
                step={
                    "name": f"write_{tc['id']}",
                    "type": "write_file",
                    "path": str(full_path),
                    "mode": mode,
                    "bytes": total,
                },
                files_created=[str(full_path)],
                round_had_write=True,
                print_line=print_line,
            )
        except Exception as e:
            print(f"     ERROR: {e}")
            return ToolResult(tool_call_id=tc["id"], name="write_file",
                              content=f"[写入失败: {e}]")
