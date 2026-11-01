"""预算耗尽后的无工具总结调用测试。"""

from skill_engine.execution.assembler import Assembler
from skill_engine.execution.executor import Executor
from skill_engine.execution.runner import Runner
from skill_engine.execution.tool_exec.budget import FINAL_SUMMARY_PREFIX
from skill_engine.models import MatchResult, Skill, SkillMetadata


class _BoundLLM:
    def __init__(self, owner, tools):
        self._owner = owner
        self._tools = tools

    def invoke(self, messages):
        return self._owner._invoke(messages, self._tools)


class RecordingLLM:
    """记录每次调用的工具集；未 bind_tools 的调用 tools=None。"""

    def __init__(self, responses, final_response):
        self.responses = list(responses)
        self.final_response = final_response
        self.calls = []

    def bind_tools(self, tools, **kwargs):
        return _BoundLLM(self, tools)

    def invoke(self, messages):
        return self._invoke(messages, None)

    def _invoke(self, messages, tools):
        self.calls.append({"tools": tools, "messages": list(messages)})
        if tools is None:
            if isinstance(self.final_response, Exception):
                raise self.final_response
            return self.final_response
        return self.responses.pop(0)


def _runner():
    executor = Executor(timeout=10)
    return Runner(Assembler(executor=executor), executor)


def _match(tmp_path):
    skill = Skill(
        metadata=SkillMetadata(name="test-final-summary", description="测试"),
        body="测试",
        directory=str(tmp_path),
    )
    return MatchResult(skill=skill, score=1.0, method="name", arguments={})


def _bash_call(i):
    return {"content": "", "tool_calls": [
        {"id": f"call_{i}", "type": "bash", "input": {"command": f"echo step_{i}"}}
    ]}


def _assert_no_dangling_tool_calls(history):
    pending = set()
    for msg in history:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            pending.update(tc["id"] for tc in msg["tool_calls"])
        elif msg.get("role") == "tool":
            pending.discard(msg.get("tool_call_id"))
    assert not pending


def test_budget_exhausted_uses_no_tools_for_summary(tmp_path):
    llm = RecordingLLM(
        [_bash_call(1), _bash_call(2)],
        {"content": "已完成：两轮检查\n验证结果：未验证\n建议下一步：补测试",
         "tool_calls": []},
    )
    result = _runner().run(_match(tmp_path), tool_dispatch=llm, max_iterations=2)

    assert result["stopped_by"] == "max_iterations"
    assert result["iterations"] == 2
    assert len(llm.calls) == 3
    assert all(call["tools"] for call in llm.calls[:-1])
    assert llm.calls[-1]["tools"] is None
    assert "自动总结" in result["output"]
    assert "建议下一步" in result["output"]
    assert FINAL_SUMMARY_PREFIX not in str(result["history"])
    assert result["history"][-1]["role"] == "assistant"
    _assert_no_dangling_tool_calls(result["history"])


def test_final_summary_ignores_tool_calls(tmp_path):
    llm = RecordingLLM(
        [_bash_call(1)],
        {"content": "", "tool_calls": [
            {"id": "late", "type": "bash", "input": {"command": "echo should_not_run"}}
        ]},
    )
    result = _runner().run(_match(tmp_path), tool_dispatch=llm, max_iterations=1)

    assert result["stopped_by"] == "max_iterations"
    assert "自动总结未返回可用内容" in result["output"]
    assert "建议下一步" in result["output"]
    assert "should_not_run" not in str(result["history"][-1])
    assert result["history"][-1]["role"] == "assistant"
    _assert_no_dangling_tool_calls(result["history"])


def test_final_summary_falls_back_on_error(tmp_path):
    llm = RecordingLLM([_bash_call(1)], RuntimeError("summary boom"))
    result = _runner().run(_match(tmp_path), tool_dispatch=llm, max_iterations=1)

    assert result["stopped_by"] == "max_iterations"
    assert "自动总结调用失败" in result["output"]
    assert "建议下一步" in result["output"]
    assert result["history"][-1]["role"] == "assistant"
    _assert_no_dangling_tool_calls(result["history"])


def test_normal_completion_does_not_add_summary_call(tmp_path):
    llm = RecordingLLM(
        [_bash_call(1), {"content": "done", "tool_calls": []}],
        {"content": "unused", "tool_calls": []},
    )
    result = _runner().run(_match(tmp_path), tool_dispatch=llm, max_iterations=5)

    assert result["stopped_by"] == "stop"
    assert len(llm.calls) == 2
    assert result["output"] == "done"
