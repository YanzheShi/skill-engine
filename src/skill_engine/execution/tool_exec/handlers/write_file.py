"""write_file：整文件写入 / 追加（安全门 + diff 预览 + 快照 + 暂存/原子转正）。

暂存策略：内容先写 ``<name>.partial``，完整度校验通过才 ``os.replace`` 转正；
不完整时目标文件保持原样（或不存在），下一轮 ``append`` 会自动续写该 partial。
开关 ``SKILLS_ENGINE_ATOMIC_WRITE=0`` 可退回"直接写目标文件"的旧行为。
"""

import json
import re
from pathlib import Path

from skill_engine.execution.paths import resolve_path
from skill_engine.execution.tool_exec import atomic_write
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.edit_patch import _render_diff
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import CALL_TRUNCATED_HINT, ToolResult

_TAIL_PREVIEW_CHARS = 60
# 分块装配的占位 marker（如 <!--@C12@-->）；残留即说明还有块没填完。
_MARKER_RE = re.compile(r"<!--@[A-Za-z0-9_-]{1,40}@-->")


def _completeness_note(path: Path, text: str) -> str:
    """给模型的完整度回执（只对能低成本判定的类型下结论）。"""
    suffix = path.suffix.lower()
    if suffix in (".html", ".htm"):
        if _MARKER_RE.search(text or ""):
            return ("complete: NO (残留占位 marker — 还有分块没填完)\n"
                    "请用 edit_file 继续填充对应 marker，不要重发整份文件")
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
        target_old = full_path.read_text(encoding="utf-8") if existed else ""
        ppath = atomic_write.partial_path(full_path)
        staging = atomic_write.enabled()

        if mode == "append":
            # 续写来源：优先未转正的 partial（上次被截断的续写目标），否则已有目标文件
            has_base = (staging and ppath.exists()) or existed
            if staging and ppath.exists():
                base = ppath.read_text(encoding="utf-8")
            else:
                base = target_old
            final_content = base
            if final_content and not final_content.endswith("\n") and content \
                    and not content.startswith("\n"):
                final_content += "\n"   # 断点续写：避免把上一行接成一行
            final_content += content
            if not has_base:
                mode = "overwrite"      # 追加目标不存在 = 新建，语义退化为覆盖
        else:
            final_content = content
            base = target_old

        # diff 预览门（仅 confirm_edits 开启时进入）
        if ctx.confirm_edits_mode in ("true", "batch"):
            diff_text = _render_diff(filepath, base, final_content)
            if not ctx.confirm_edit("write_file", str(full_path), diff_text):
                print(f"     WRITE REJECTED by user: {filepath}")
                return ToolResult(
                    tool_call_id=tc["id"], name="write_file",
                    content="[用户拒绝了本次写入] 文件未变更。请调整方案或与用户澄清需求。",
                )

        try:
            work_path = ppath if staging else full_path
            work_path.parent.mkdir(parents=True, exist_ok=True)
            work_path.write_text(final_content, encoding="utf-8")

            verdict = _completeness_note(full_path, final_content)
            truncated = bool(getattr(ctx, "turn_truncated", False))
            finalized = True
            if staging:
                finalized = atomic_write.should_finalize(verdict, truncated)
                if finalized:
                    if existed:
                        ctx.snapshot.record(full_path, target_old)
                    atomic_write.finalize(ppath, full_path)
                    ctx.file_tracker.on_write(full_path)
            else:
                if existed:
                    ctx.snapshot.record(full_path, target_old)
                ctx.file_tracker.on_write(full_path)

            total = len(final_content)
            lines = final_content.count("\n") + (
                0 if not final_content or final_content.endswith("\n") else 1)
            tail = final_content[-_TAIL_PREVIEW_CHARS:].replace("\n", "\\n")
            target_desc = filepath if finalized else f"{filepath}.partial"
            state = (f"file is now {total} bytes / {lines} lines"
                     if finalized else
                     f"staged {total} bytes; target NOT written (incomplete)")
            note = ""
            if staging and truncated and not finalized:
                note = ("\n本轮输出撞到 max_tokens（finish_reason=length）→ "
                        "内容已暂存，目标文件未改动；请继续用 append / edit_file 补齐，"
                        "不要重发整份。")
            receipt = (
                f"wrote {len(content)} bytes to {target_desc} (mode={mode}); "
                f"{state}\n"
                f"tail: ...{tail}\n"
                + verdict + note
            )
            summary = (f"{'wrote' if finalized else 'staged'} {len(content)} bytes → "
                       f"{target_desc} ({mode}); {total} bytes / {lines} lines · "
                       f"{verdict.splitlines()[0]}")
            emit = getattr(ctx, "emit_result", None)
            if emit is not None:
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
                    "staged": not finalized,
                },
                files_created=[str(full_path)] if finalized else [],
                round_had_write=finalized,
                print_line=print_line,
            )
        except Exception as e:
            print(f"     ERROR: {e}")
            return ToolResult(tool_call_id=tc["id"], name="write_file",
                              content=f"[写入失败: {e}]")
