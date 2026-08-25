"""图片托管：把本地图片转换为 VLM 可读取的 URL。

背景（已实测纠正）：商汤 SenseNova（sensenova-6.8-flash-lite）等模型
**支持 base64 data URL 内联**，反而是**公网 URL 拉取受限**（服务器侧出口
受限 → HTTP400 image download failed）。note-assistant 与 skill-engine 两处
独立实测（verify_base64_vlm.py）均证实这一点。

因此默认策略反转（见 resolve_image_url）：
- 优先 base64 内联 data URL（稳定、无需外部依赖、不依赖网络出口）；
- R2 公网 URL 作为**可选回退**：仅当显式开启 USE_R2_FOR_IMAGES 且上传成功时
  才使用，用于个别确实只吃公网 URL 的模型/网关。

设计原则：
- fail-soft：未配置 R2 或上传失败一律退回 base64 内联，不影响原链路。
- 零依赖：只用 stdlib（urllib），不引入 boto3。
- key 每次唯一（uuid），避免模型/CDN 按 URL 缓存旧图。
- 会话内按 (路径, 大小, mtime) 缓存，相同文件不重复编码/上传。
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid

# 确保 config.yml 的 r2_* settings 回填进环境变量（本模块可能被独立导入）
import skill_engine.config  # noqa: F401,E402

CF_API = "https://api.cloudflare.com/client/v4"

_ENV_KEYS = ("CF_R2_TOKEN", "CF_R2_ACCOUNT_ID", "CF_R2_BUCKET", "CF_R2_PUBLIC_BASE")

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# 是否把图片托管到 R2 换取公网 URL。默认关闭——因为实测表明 SenseNova 等
# 模型支持 base64 内联、拉不到公网 URL；开启反而可能导致识别失败。
# 仅当某个模型/网关确实只吃公网 URL 时才置 1。
USE_R2_FOR_IMAGES = os.getenv("SKILL_ENGINE_USE_R2_IMAGES", "0").strip() in ("1", "true", "yes")


def r2_config() -> dict | None:
    """读取 R2 配置（环境变量，config.yml settings 段会回填）。

    Returns:
        四项齐全时返回 {CF_R2_TOKEN, CF_R2_ACCOUNT_ID, CF_R2_BUCKET, CF_R2_PUBLIC_BASE}；
        任一项缺失/为空返回 None（调用方走 base64 内联回退）。
    """
    vals = {k: os.getenv(k, "").strip() for k in _ENV_KEYS}
    return vals if all(vals.values()) else None


def upload_image_to_r2(data: bytes, mime: str) -> str | None:
    """上传图片字节到 R2，返回公网 URL。

    Args:
        data: 图片原始字节（建议先压缩再上传，省流量/省拉取时间）。
        mime: Content-Type（image/png 等）。

    Returns:
        公网 URL（https://<public_base>/vision/...）；未配置或任何失败返回 None。
    """
    cfg = r2_config()
    if cfg is None:
        return None
    ext = _MIME_EXT.get(mime, ".png")
    key = f"vision/{time.strftime('%Y%m%d')}/{uuid.uuid4().hex}{ext}"
    url = (
        f"{CF_API}/accounts/{cfg['CF_R2_ACCOUNT_ID']}"
        f"/r2/buckets/{cfg['CF_R2_BUCKET']}/objects/{key}"
    )
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={"Authorization": f"Bearer {cfg['CF_R2_TOKEN']}",
                 "Content-Type": mime},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
        if resp.get("success"):
            return f"{cfg['CF_R2_PUBLIC_BASE'].rstrip('/')}/{key}"
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    return None


def resolve_image_url(
    payload: bytes,
    mime: str,
    *,
    b64: str | None = None,
    cache: dict | None = None,
    cache_key: object | None = None,
) -> tuple[str, str]:
    """把图片字节解析为 VLM 可读取的 URL，返回 (url, note)。

    策略（已实测纠正，见模块 docstring）：
    1. 默认 **base64 内联** data URL——SenseNova 等模型支持且更稳；
    2. 仅当 USE_R2_FOR_IMAGES=1 且 R2 配置齐全且上传成功时，改用公网 R2 URL
       （个别只吃公网 URL 的模型/网关可用）；
    3. 任何失败一律 fail-soft 退回 base64 内联。

    Args:
        payload: 图片原始字节（已压缩/缩放后）。
        mime: Content-Type（image/png 等）。
        b64: 预先算好的 base64 串；不传则内部按 payload 计算。
        cache: 会话级缓存 dict（可选），命中则直接返回缓存 URL。
        cache_key: 缓存键（可选），通常为 (path, size, mtime_ns)。

    Returns:
        (url, note): url 为最终注入 message 的 URL 字符串；
        note 为给用户的说明（如「，已上传 R2 公网 URL」或空串）。
    """
    if b64 is None:
        b64 = base64.b64encode(payload).decode("ascii")
    inline = f"data:{mime};base64,{b64}"

    # 命中缓存：相同文件（大小+mtime 不变）会话内只解析一次
    if cache is not None and cache_key is not None and cache_key in cache:
        cached = cache[cache_key]
        return cached, ("" if cached == inline else "，已上传 R2 公网 URL")

    url, note = inline, ""

    # 仅在显式开启 R2 时尝试公网 URL（默认不开启 → 始终走 base64）
    if USE_R2_FOR_IMAGES:
        try:
            got = upload_image_to_r2(payload, mime)
            if got:
                url, note = got, "，已上传 R2 公网 URL"
        except Exception:
            url, note = inline, ""

    if cache is not None and cache_key is not None:
        cache[cache_key] = url
    return url, note
