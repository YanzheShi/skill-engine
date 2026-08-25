"""验证 SenseNova VLM 能否通过 base64 data URL 内联识别本地图片。

背景：skill-engine 的 image_hosting.py 假设 sensenova-6.8-flash-lite
「不接受 base64 内联、只吃公网 URL」，因此做了 R2 外链上传。
本脚本直接把 demo 最小图编码为 data:image/png;base64,... 发给 vlm
profile，验证该假设是否成立。

用法：env -u PYTHONPATH .venv/Scripts/python.exe verify_base64_vlm.py
"""

import base64
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage

import skill_engine.config as cfg_mod
from skill_engine.config import get_llm_by_profile, MODEL_PROFILES

# demo 中体积最小的图（8487 字节）
IMG = Path("demo/moa-VLM指导LLM根据设计稿完成ui开发/交付文件2.png")


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else "vlm"
    if not IMG.exists():
        raise SystemExit(f"[ERR] 找不到测试图: {IMG}")

    profiles = list(MODEL_PROFILES.keys())
    print(f"[info] 可用 profile: {profiles}")
    if profile not in MODEL_PROFILES:
        raise SystemExit(f"[ERR] config 中无 '{profile}' profile")

    data = IMG.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"
    print(f"[info] image bytes={len(data)}  b64_len={len(b64)}  data_url_prefix={data_url[:40]}...")

    model = get_llm_by_profile(profile)
    print(f"[info] 已实例化 '{profile}' 模型: model={getattr(model, 'model_name', model)}  vision_flag={MODEL_PROFILES[profile].get('vision')}")

    msg = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": data_url}},
        {"type": "text", "text": "请简要描述这张图片展示的内容（一句话，中文）。"},
    ])

    print("[info] 正在调用 VLM（base64 data URL 内联）...")
    resp = model.invoke([msg])

    print("=== VLM 原始返回 ===")
    print(repr(resp))
    print("=== content ===")
    print(resp.content)
    # sensenova 推理模型可能把内容放在 reasoning / additional_kwargs
    extra = getattr(resp, "additional_kwargs", None)
    if extra:
        print("=== additional_kwargs ===")
        print(extra)


if __name__ == "__main__":
    main()
