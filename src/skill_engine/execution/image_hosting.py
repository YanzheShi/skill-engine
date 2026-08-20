"""图片外链托管：上传到 Cloudflare R2 并返回公网 URL。

背景：部分多模态模型（如 sensenova-6.8-flash-lite）不接受 base64 内联图片，
只吃公网 URL。本模块把本地图片上传到 R2 bucket，换取公网 URL 供模型拉取。

设计原则：
- fail-soft：未配置 R2 或上传失败一律返回 None，调用方退回 base64 内联，不影响原链路。
- 零依赖：只用 stdlib（urllib），不引入 boto3。
- key 每次唯一（uuid），避免模型/CDN 按 URL 缓存旧图。
"""

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
