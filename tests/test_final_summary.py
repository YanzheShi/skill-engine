"""预算耗尽后的无工具总结调用测试。"""

from skill_engine.execution.assembler import Assembler
from skill_engine.execution.executor import Executor
from skill_engine.execution.runner import Runner
from skill_engine.execution.tool_exec.budget import (
    FINAL_SUMMARY_PREFIX,
    STOP_SUMMARY_PREFIX,
    _change_manifest,
)
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


def _write_call(path="a.txt", body="hello"):
    return {"content": "", "tool_calls": [
        {"id": "w1", "type": "write_file",
         "input": {"path": path, "content": body}}
    ]}


def _stop_call(reason, content="\n\n"):
    return {"content": content, "tool_calls": [
        {"id": "s1", "type": "stop", "input": {"reason": reason}}
    ]}


def test_stop_with_empty_content_gets_closing_summary_and_manifest(tmp_path):
    """复现线上问题：stop 正文为空时，不能只把一行 reason 当最终答复。"""
    llm = RecordingLLM(
        [_write_call(), _stop_call("已核对并小幅更新文档，验证通过")],
        {"content": "本次修改了 a.txt，并跑过基线校验", "tool_calls": []},
    )
    result = _runner().run(_match(tmp_path), tool_dispatch=llm)

    assert result["stopped_by"] == "tool_stop"
    assert llm.calls[-1]["tools"] is None          # 收尾走无工具通道
    out = result["output"]
    assert "本次修改了 a.txt" in out                # 不再是孤零零一行 reason
    assert "本次文件改动" in out and "新建 a.txt" in out
    assert STOP_SUMMARY_PREFIX not in str(result["history"])   # 临时提示已 pop
    _assert_no_dangling_tool_calls(result["history"])


def test_stop_with_content_appends_manifest_without_extra_call(tmp_path):
    """正文非空时不该多花一次 LLM 调用，但仍要附产物清单。"""
    llm = RecordingLLM(
        [_write_call(), _stop_call("完成", content="我把 a.txt 写好了")],
        {"content": "不该被调用", "tool_calls": []},
    )
    result = _runner().run(_match(tmp_path), tool_dispatch=llm)

    assert len(llm.calls) == 2
    out = result["output"]
    assert out.startswith("我把 a.txt 写好了")
    assert "本次文件改动" in out and "a.txt" in out
    assert "不该被调用" not in out


def test_change_manifest_groups_and_labels():
    steps = [
        {"type": "read_file", "path": "/p/x.py"},
        {"type": "write_file", "path": "/p/new.py", "bytes": 42, "created": True},
        {"type": "edit_file", "path": "/p/old.py", "edits_count": 3},
        {"type": "write_file", "path": "/p/big.html", "bytes": 10,
         "created": True, "staged": True},
    ]
    text = _change_manifest(steps)
    assert "新建 /p/new.py（42 字节）" in text
    assert "修改 /p/old.py（3 处编辑）" in text
    assert "未转正" in text and "big.html" in text
    assert "read_file" not in text
    assert "合计 3 个文件" in text
    assert _change_manifest([{"type": "bash", "path": None}]) == ""
