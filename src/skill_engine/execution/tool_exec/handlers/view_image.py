"""view_image：把图片加载进对话（视觉模型注入多模态消息，文本模型回提示）。"""

from skill_engine.execution.paths import resolve_path
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class ViewImageHandler(BaseHandler):
    name = "view_image"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        filepath = tc["input"].get("path", "")

        # 安全门（只查路径，strict 不 BLOCK）
        approved, err_msg = ctx.check_file_safety("read", filepath, ctx.skill)
        if not approved:
            print(f"     {err_msg}")
            return ToolResult(tool_call_id=tc["id"], name="view_image", content=err_msg)

        full_path = resolve_path(filepath, ctx.base_dir)
        if not full_path.is_file():
            print(f"     view_image FILE NOT FOUND: {filepath}")
            return ToolResult(tool_call_id=tc["id"], name="view_image",
                              content=f"[图片不存在: {filepath}]")

        # 模态区分：仅 vision 模型注入图片；文本模型返回提示（省 token）
        from skill_engine.config import model_supports_vision
        if not model_supports_vision(ctx.llm):
            notice = (
                f"[当前模型为文本模态，无法查看图片] {filepath} 是图片文件"
                f"（{full_path.stat().st_size} bytes）。请由支持视觉的模型"
                f"（vision: true）查看，或手动打开文件确认。"
            )
            if ctx.emit_tool:
                ctx.emit_tool(f"view_image {filepath}")
            if ctx.emit_result:
                ctx.emit_result(notice)
            return ToolResult(tool_call_id=tc["id"], name="view_image", content=notice)

        try:
            import base64
            ext = full_path.suffix.lower()
            mime = {".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".gif": "image/gif",
                    ".webp": "image/webp"}.get(ext, "image/png")
            # 进模型前自适应压缩：按"面积线性计费"决策（sensenova 实测：
            # image_tokens ≈ 面积(px)/1024，纯线性、无瓦片取整边界）。
            # 只要发生缩放，面积必然严格减小 → token 必然减少，直接采纳；
            # 未缩放（最长边 ≤ 1568）则保持原样，避免无谓重编码损失画质。
            # 缩放后取 JPEG/PNG 较小者；Pillow 缺失或解码失败回退原样字节（零新硬依赖）。
            import io
            raw_bytes = full_path.read_bytes()
            orig_size = len(raw_bytes)
            size = orig_size
            note = ""
            payload = raw_bytes  # 最终发送字节（压缩后或原样）
            try:
                from PIL import Image
                try:
                    resample = Image.Resampling.LANCZOS
                except AttributeError:
                    resample = Image.LANCZOS
                img = Image.open(io.BytesIO(raw_bytes))
                if max(img.size) > 1568:
                    scale = 1568 / max(img.size)
                    img = img.resize(
                        (int(img.size[0] * scale), int(img.size[1] * scale)),
                        resample,
                    )
                    if img.mode in ("RGBA", "P", "LA"):
                        img = img.convert("RGB")
                    # 缩放后面积减小→token 减少；取 JPEG/PNG 较小者
                    buf_jpeg = io.BytesIO()
                    img.save(buf_jpeg, format="JPEG", quality=85)
                    pj = buf_jpeg.getvalue()
                    buf_png = io.BytesIO()
                    img.save(buf_png, format="PNG")
                    pp = buf_png.getvalue()
                    if len(pj) <= len(pp):
                        payload = pj
                        mime = "image/jpeg"
                        size = len(pj)
                        note = f"（压缩至 {size} bytes，原 {orig_size} bytes）"
                    else:
                        payload = pp
                        mime = "image/png"
                        size = len(pp)
                        note = f"（缩放至 {size} bytes，原 {orig_size} bytes）"
            except Exception:
                pass
            b64 = base64.b64encode(payload).decode("ascii")
            # 图片注入策略（实测纠正）：默认 base64 内联，SenseNova 等模型
            # 支持 base64 且拉不到公网 URL；仅当显式开启 R2 时才回退公网 URL。
            # 见 skill_engine.execution.image_hosting.resolve_image_url。
            try:
                from skill_engine.execution.image_hosting import (
                    resolve_image_url,
                )
                cache_key = (str(full_path), orig_size,
                             int(full_path.stat().st_mtime_ns))
                url, url_note = resolve_image_url(
                    payload, mime,
                    b64=b64,
                    cache=ctx.image_url_cache,
                    cache_key=cache_key,
                )
            except Exception:
                url = f"data:{mime};base64,{b64}"
                url_note = ""
            # 先落 tool 文本消息（保持 OpenAI 协议顺序），
            # 再注入 user 多模态消息：下一轮调用时模型即可"看见"图片
            if ctx.emit_tool:
                ctx.emit_tool(f"view_image {filepath}")
            if ctx.emit_result:
                ctx.emit_result(f"图片已注入多模态消息（{size} bytes, {mime}{note}{url_note}）")
            return ToolResult(
                tool_call_id=tc["id"], name="view_image",
                content=(f"[图片已加载] {filepath}（{size} bytes{note}）"
                         f"，多模态消息已注入{url_note}。"),
                extra_messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text",
                         "text": f"已加载图片 {filepath}（{size} bytes{note}），请仔细查看图片内容，"
                                 "据此检查实现是否符合要求。"},
                        {"type": "image_url",
                         "image_url": {"url": url}},
                    ],
                }],
            )
        except Exception as e:
            print(f"     view_image ERROR: {e}")
            return ToolResult(tool_call_id=tc["id"], name="view_image",
                              content=f"[读取图片失败: {e}]")
