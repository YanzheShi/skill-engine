"""vision 标记机制测试：setattr 成功 / pydantic-forbid 兜底注册表 / 包装链。

背景：langchain 新版模型（ChatOpenAI 等）是 pydantic v2 + extra=forbid，
运行期 ``model.vision = True`` 会抛 ValueError——曾直接导致 MOA 全部
model_config_error（严重回归）。此处守住该修复。
"""


class _SlotsOnly:
    """模拟 pydantic v2 extra=forbid：不容许运行时 setattr 未声明字段。"""

    __slots__ = ("x",)

    def __init__(self):
        self.x = 1


class _Plain:
    pass


def test_mark_model_vision_plain_object_setattr():
    from skill_engine.config import _mark_model_vision, model_supports_vision
    obj = _Plain()
    _mark_model_vision(obj, True)
    assert obj.vision is True
    assert model_supports_vision(obj) is True


def test_mark_model_vision_slots_object_registry():
    """setattr 失败（forbid 类）→ 落注册表，查询仍为 True。"""
    from skill_engine.config import _mark_model_vision, model_supports_vision
    obj = _SlotsOnly()
    _mark_model_vision(obj, True)
    assert not hasattr(obj, "vision")
    assert model_supports_vision(obj) is True


def test_model_supports_vision_through_counting_llm_chain():
    """CountingLLM 包装后（__getattr__ 透传不到时）沿 _llm 链仍能查到。"""
    from skill_engine.config import _mark_model_vision, model_supports_vision
    from skill_engine.execution.counting_llm import CountingLLM
    inner = _SlotsOnly()
    _mark_model_vision(inner, True)
    wrapped = CountingLLM(inner, {"calls": 0})
    assert model_supports_vision(wrapped) is True


def test_model_supports_vision_false_cases():
    from skill_engine.config import _mark_model_vision, model_supports_vision
    assert model_supports_vision(_Plain()) is False
    assert model_supports_vision(None) is False
    obj = _Plain()
    _mark_model_vision(obj, False)
    assert model_supports_vision(obj) is False


def test_get_llm_by_profile_no_longer_crashes(monkeypatch):
    """回归：get_llm_by_profile 实例化 forbid 类模型不得抛异常。"""
    import skill_engine.config as cfg

    class ForbidModel(_SlotsOnly):
        def invoke(self, *a, **k):
            return "ok"

    def fake_init(**kw):
        return ForbidModel()

    monkeypatch.setattr(cfg, "MODEL_PROFILES", {
        "default": {"model": "gpt-4o", "model_provider": "openai",
                    "base_url": "", "api_key": "x", "vision": True},
    })
    monkeypatch.setattr(cfg, "init_chat_model", fake_init)
    llm = cfg.get_llm_by_profile("default")
    assert cfg.model_supports_vision(llm) is True


# ── LLM 调用显式超时（防 openai 默认 600s 静默等待） ─────────────────────────
def test_apply_call_timeout_openai_provider():
    from skill_engine.config import _apply_call_timeout, LLM_CALL_TIMEOUT
    cfg = {"model_provider": "openai", "model": "x"}
    _apply_call_timeout(cfg)
    assert cfg["request_timeout"] == LLM_CALL_TIMEOUT
    # 已有值不被覆盖
    cfg = {"model_provider": "openai", "request_timeout": 30}
    _apply_call_timeout(cfg)
    assert cfg["request_timeout"] == 30


def test_apply_call_timeout_anthropic_provider():
    from skill_engine.config import _apply_call_timeout, LLM_CALL_TIMEOUT
    cfg = {"model_provider": "anthropic", "model": "x"}
    _apply_call_timeout(cfg)
    assert cfg["timeout"] == LLM_CALL_TIMEOUT


def test_apply_call_timeout_unknown_provider_untouched():
    from skill_engine.config import _apply_call_timeout
    cfg = {"model_provider": "sensenova", "model": "x"}
    _apply_call_timeout(cfg)
    assert "request_timeout" not in cfg
    assert "timeout" not in cfg


def test_get_llm_by_profile_injects_request_timeout(monkeypatch):
    """MOA 实例化模型时必须带上显式网络超时。"""
    import skill_engine.config as cfg

    seen = {}

    class FakeLLM:
        def __init__(self, **kw):
            seen.update(kw)

        def invoke(self, *a, **k):
            return "ok"

    def fake_init(**kw):
        return FakeLLM(**kw)

    monkeypatch.setattr(cfg, "MODEL_PROFILES", {
        "default": {"model": "gpt-4o", "model_provider": "openai",
                    "base_url": "", "api_key": "x"},
    })
    monkeypatch.setattr(cfg, "init_chat_model", fake_init)
    cfg.get_llm_by_profile("default")
    assert seen.get("request_timeout") == cfg.LLM_CALL_TIMEOUT


def test_parse_model_entries_keeps_vision_flag():
    from skill_engine.config import _parse_model_entries
    out = _parse_model_entries([
        {"name": "a", "model": "m", "api_key": "k", "vision": True},
        {"name": "b", "model": "m", "api_key": "k"},
    ])
    assert out["a"]["vision"] is True
    assert out["b"]["vision"] is False