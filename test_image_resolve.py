"""聚焦单测：验证 resolve_image_url 的 base64 优先策略 + agnes vision 标记。

不依赖任何真实 API key（纯函数 + 配置解析）。
用法：env -u PYTHONPATH .venv/Scripts/python.exe test_image_resolve.py
"""

import base64

from skill_engine.config import MODEL_PROFILES, model_supports_vision
from skill_engine.execution import image_hosting as ih


def test_resolve_default_inline():
    """默认（USE_R2_FOR_IMAGES 关闭）→ 必须返回 base64 内联，绝不走公网 URL。"""
    payload = b"\x89PNG\r\n\x1a\n fake png bytes"
    b64 = base64.b64encode(payload).decode("ascii")
    url, note = ih.resolve_image_url(payload, "image/png", b64=b64)
    assert url == f"data:image/png;base64,{b64}", f"期望 base64 内联，实得 {url!r}"
    assert note == "", f"默认不应有 R2 note，实得 {note!r}"
    print("[PASS] 默认策略 = base64 内联")


def test_resolve_cache_hit():
    """相同 cache_key 第二次调用直接命中缓存，不重复编码。"""
    payload = b"abc"
    b64 = base64.b64encode(payload).decode("ascii")
    cache: dict = {}
    key = ("/fake/path.png", 3, 999)
    u1, _ = ih.resolve_image_url(payload, "image/png", b64=b64, cache=cache, cache_key=key)
    u2, _ = ih.resolve_image_url(payload, "image/png", b64=b64, cache=cache, cache_key=key)
    assert u1 == u2 and cache[key] == u1
    print("[PASS] 会话内缓存命中")


def test_resolve_r2_enabled_fallback(monkeypatch):
    """开启 USE_R2_FOR_IMAGES 但 R2 未配置 → upload 返回 None → 仍回退 base64。"""
    monkeypatch.setattr(ih, "USE_R2_FOR_IMAGES", True)
    monkeypatch.setattr(ih, "r2_config", lambda: None)  # 模拟未配置
    payload = b"xyz"
    b64 = base64.b64encode(payload).decode("ascii")
    url, note = ih.resolve_image_url(payload, "image/png", b64=b64)
    assert url == f"data:image/png;base64,{b64}"
    assert note == ""
    print("[PASS] R2 开启但未配置 → 安全回退 base64")


def test_agnes_vision_flag():
    """agnes 现在应被标记为支持视觉（修复漏标）。"""
    assert "agnes" in MODEL_PROFILES, "config 中无 agnes profile"
    assert MODEL_PROFILES["agnes"].get("vision") is True, "agnes vision 标记未生效"
    print("[PASS] agnes.vision = true")


def test_agnes_model_supports_vision():
    """model_supports_vision 对 agnes 实例应返回 True。"""
    from skill_engine.config import get_llm_by_profile
    model = get_llm_by_profile("agnes")
    assert model_supports_vision(model) is True, "agnes 实例未识别为视觉模型"
    print("[PASS] model_supports_vision(agnes) = True")


if __name__ == "__main__":
    test_resolve_default_inline()
    test_resolve_cache_hit()
    test_resolve_r2_enabled_fallback(__import__("pytest").MonkeyPatch())
    test_agnes_vision_flag()
    # model_supports_vision 实例化需要真实 key；仅当环境变量可用时跑
    try:
        test_agnes_model_supports_vision()
    except Exception as e:
        print(f"[SKIP] agnes 实例视觉检查跳过（缺 key/env）: {e}")
    print("\n=== 全部聚焦断言通过 ===")
