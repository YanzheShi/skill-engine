"""
verify_command 自动验证钩子测试（P0 S0-4b）

覆盖：
- _extract_test_failures：pytest 风格 FAILED/ERROR 清单提取
- 轮内写盘后自动跑 verify_command：失败时结构化信号回灌（含失败清单）
- 验证通过时不打扰（无回灌消息）
- 轮内无写盘时不跑验证
"""

import pytest
from pathlib import Path

from skill_engine.execution.tool_dispatch import _extract_test_failures


class TestExtractTestFailures:
    def test_extracts_failed_lines(self):
        out = "running...\nFAILED tests/a.py::test_x - assert 1 == 2\nFAILED tests/b.py::test_y\nok"
        fails = _extract_test_failures(out)
        assert len(fails) == 2
        assert fails[0].startswith("FAILED tests/a.py::test_x")

    def test_cap_at_20(self):
        out = "\n".join(f"FAILED t.py::test_{i}" for i in range(30))
        assert len(_extract_test_failures(out)) == 20

    def test_empty(self):
        assert _extract_test_failures("") == []
        assert _extract_test_failures("all passed") == []


class _ScriptedLLM:
    def __init__(self, steps):
        self.steps = list(steps)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if self.steps:
            return self.steps.pop(0)
        return {"content": "完成", "tool_calls": []}


def _step(i, tool, args):
    return {"content": "", "tool_calls": [{"id": f"c{i}", "type": tool, "input": args}]}


def _make(tmp_path, verify_command):
    from skill_engine.models import Skill, SkillMetadata, MatchResult
    from skill_engine.execution.executor import Executor
    from skill_engine.execution.assembler import Assembler
    from skill_engine.execution.tool_dispatch import ToolDispatchRunner

    skill = Skill(metadata=SkillMetadata(name="demo", description="d",
                                         verify_command=verify_command),
                  body="", directory=str(tmp_path))
    ex = Executor(timeout=15, allow_all=True)
    runner = ToolDispatchRunner(executor=ex, assembler=Assembler(executor=ex),
                                working_root=str(tmp_path))
    match = MatchResult(skill=skill, score=1.0, method="name", arguments={})
    return runner, match


def _user_msgs(result):
    return [m.get("content", "") for m in result.get("history", [])
            if m.get("role") == "user"]


class TestVerifyHook:
    def test_failure_feeds_back_structured_signal(self, tmp_path):
        (tmp_path / "verify_fail.py").write_text(
            "import sys\n"
            "print('FAILED tests/x.py::test_a - assert 1 == 2')\n"
            "sys.exit(1)\n", encoding="utf-8")
        runner, match = _make(tmp_path, "python verify_fail.py")
        llm = _ScriptedLLM([
            _step(1, "write_file", {"path": "n.py", "content": "x = 1\n"}),
        ])
        result = runner.run(match, llm, max_iterations=4)
        users = _user_msgs(result)
        feedback = [u for u in users if "[自动验证失败]" in u]
        assert len(feedback) == 1
        assert "失败清单" in feedback[0]
        assert "FAILED tests/x.py::test_a" in feedback[0]
        assert "exit_code: 1" in feedback[0]

    def test_success_no_feedback(self, tmp_path):
        (tmp_path / "verify_ok.py").write_text(
            "print('all good')\n", encoding="utf-8")
        runner, match = _make(tmp_path, "python verify_ok.py")
        llm = _ScriptedLLM([
            _step(1, "write_file", {"path": "n.py", "content": "x = 1\n"}),
        ])
        result = runner.run(match, llm, max_iterations=4)
        assert not any("[自动验证失败]" in u for u in _user_msgs(result))

    def test_no_write_no_verify(self, tmp_path):
        # 验证脚本若被执行会留下标记文件；只读轮次不应触发
        (tmp_path / "verify_marker.py").write_text(
            "from pathlib import Path\nPath('MARKER').write_text('ran')\n",
            encoding="utf-8")
        target = tmp_path / "readme.txt"
        target.write_text("hello\n", encoding="utf-8")
        runner, match = _make(tmp_path, "python verify_marker.py")
        llm = _ScriptedLLM([
            _step(1, "read_file", {"path": "readme.txt"}),
        ])
        runner.run(match, llm, max_iterations=4)
        assert not (tmp_path / "MARKER").exists()

    def test_no_verify_command_configured(self, tmp_path):
        runner, match = _make(tmp_path, "")
        llm = _ScriptedLLM([
            _step(1, "write_file", {"path": "n.py", "content": "x = 1\n"}),
        ])
        result = runner.run(match, llm, max_iterations=4)
        assert result.get("stopped_by") == "stop"
        assert not any("[自动验证失败]" in u for u in _user_msgs(result))
