"""view_image 工具测试：模态区分 + 多模态注入。

- 文本模型（无 vision 标记）：不注入图片，返回可读提示；
- vision 模型：图片以 user 消息（image_url data URI）注入，下一轮调用可见。
"""

import pytest

from skill_engine.models import Skill, SkillMetadata, MatchResult


class MockLLM:
    """模拟会吐 tool_call 的 LLM（dict 风格，无 bind_tools）"""

    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0
        self.last_messages = []
        self.vision = False

    def invoke(self, messages):
        self.call_count += 1
        self.last_messages = messages
        resp = self.responses[self.call_count - 1]
        if isinstance(resp, str):
            return {"content": resp, "tool_calls": []}
        return resp


def _runner():
    from skill_engine.execution.runner import Runner
    from skill_engine.execution.executor import Executor
    from skill_engine.execution.assembler import Assembler
    return Runner(Assembler(executor=Executor(timeout=10)), Executor(timeout=10))


def _png(tmp_path) -> str:
    p = tmp_path / "shot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 20)
    return str(p)


def _match(tmp_path) -> MatchResult:
    skill = Skill(metadata=SkillMetadata(name="t", description="d"),
                  body="b", directory=str(tmp_path))
    return MatchResult(skill=skill, score=1.0, method="name", arguments={})


def _view_call(png: str) -> dict:
    return {"content": "", "tool_calls": [
        {"id": "c1", "type": "view_image", "input": {"path": png}}]}


def _tool_msgs(llm) -> list[dict]:
    return [m for m in llm.last_messages if m.get("role") == "tool"]


def test_view_image_text_model_returns_notice(tmp_path):
    """文本模型（无 vision）：返回提示，不注入图片消息。"""
    runner = _runner()
    llm = MockLLM([_view_call(_png(tmp_path)), "done"])
    runner.run(_match(tmp_path), tool_dispatch=llm)
    tool_msgs = _tool_msgs(llm)
    assert any("无法查看图片" in m.get("content", "") for m in tool_msgs)
    multimodal = [m for m in llm.last_messages if isinstance(m.get("content"), list)]
    assert multimodal == []


def test_view_image_vision_model_injects_multimodal(tmp_path):
    """vision 模型：图片以 user 消息 image_url data URI 注入。"""
    runner = _runner()
    llm = MockLLM([_view_call(_png(tmp_path)), "done"])
    llm.vision = True
    result = runner.run(_match(tmp_path), tool_dispatch=llm)
    assert "无法查看图片" not in result["output"]
    multimodal = [m for m in llm.last_messages
                  if isinstance(m.get("content"), list)]
    assert multimodal
    parts = multimodal[0]["content"]
    urls = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
    assert urls
    assert urls[0].startswith("data:image/png;base64,")


def test_view_image_missing_file(tmp_path):
    """图片不存在时返回明确错误。"""
    runner = _runner()
    llm = MockLLM([
        {"content": "", "tool_calls": [
            {"id": "c1", "type": "view_image", "input": {"path": str(tmp_path / "nope.png")}}]},
        "done",
    ])
    runner.run(_match(tmp_path), tool_dispatch=llm)
    tool_msgs = _tool_msgs(llm)
    assert any("图片不存在" in m.get("content", "") for m in tool_msgs)


# ── 按模型能力过滤视觉工具（能力边界） ─────────────────────────────────────
class _CaptureBindLLM(MockLLM):
    """记录 bind_tools 收到的工具名列表（验证能力过滤）。"""

    def __init__(self, responses, vision=False):
        super().__init__(responses)
        self.vision = vision
        self.bound_names = []

    def bind_tools(self, tools, **kwargs):
        self.bound_names = [t.name for t in tools]
        return self


def test_text_model_does_not_get_vision_tools(tmp_path):
    """文本模型：view_image / shot_web 不进入工具列表，从根源杜绝无效步骤。"""
    runner = _runner()
    llm = _CaptureBindLLM(["done"], vision=False)
    runner.run(_match(tmp_path), tool_dispatch=llm)
    assert "view_image" not in llm.bound_names
    assert "shot_web" not in llm.bound_names
    assert "read_file" in llm.bound_names      # 常规工具不受影响


def test_vision_model_keeps_vision_tools(tmp_path):
    """视觉模型：保留 view_image / shot_web。"""
    runner = _runner()
    llm = _CaptureBindLLM(["done"], vision=True)
    runner.run(_match(tmp_path), tool_dispatch=llm)
    assert "view_image" in llm.bound_names
    assert "shot_web" in llm.bound_names


def test_text_model_prompt_declares_capability(tmp_path):
    """文本模型：首条 prompt 注入能力声明（无视觉、不要截图看图）。"""
    runner = _runner()
    llm = _CaptureBindLLM(["done"], vision=False)
    runner.run(_match(tmp_path), tool_dispatch=llm)
    first_user = next(m for m in llm.last_messages if m.get("role") == "user")
    assert "无视觉的文本模型" in first_user["content"]
    assert "view_image" in first_user["content"]


def test_vision_model_prompt_no_capability_notice(tmp_path):
    """视觉模型：不注入文本模态声明。"""
    runner = _runner()
    llm = _CaptureBindLLM(["done"], vision=True)
    runner.run(_match(tmp_path), tool_dispatch=llm)
    first_user = next(m for m in llm.last_messages if m.get("role") == "user")
    assert "无视觉的文本模型" not in first_user["content"]
