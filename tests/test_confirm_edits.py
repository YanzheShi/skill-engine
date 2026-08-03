"""
编辑 diff 预览与确认测试（S1-3，P0 收尾项）

覆盖：
- _render_diff：基本 diff / 超长截断 / 无变化
- confirm_edits="true"：逐次确认（拒绝不落盘、批准落盘、每次都问）
- confirm_edits="batch"：逐文件确认（首改询问、同文件自动放行、换文件再问）
- 默认关闭：不询问（对其他 skill 零影响）
- 无 human_io：降级为仅展示、不阻断
"""

import pytest
from pathlib import Path

from skill_engine.execution.tool_dispatch import _render_diff


class TestRenderDiff:
    def test_basic_diff(self):
        out = _render_diff("a.py", "x = 1\ny = 2\n", "x = 1\ny = 3\n")
        assert "-y = 2" in out
        assert "+y = 3" in out
        assert "a.py (before)" in out

    def test_truncates_huge_diff(self):
        old = "\n".join(f"line {i}" for i in range(500))
        new = "\n".join(f"LINE {i}" for i in range(500))
        out = _render_diff("big.py", old, new)
        assert "仅显示前" in out

    def test_no_change(self):
        assert _render_diff("a.py", "same\n", "same\n") == "(内容无变化)"


class _FakeHumanIO:
    """记录 emit 内容、按脚本回答 read 的假 human_io。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.emitted = []
        self.asked = 0

    def emit(self, text):
        self.emitted.append(text)

    def read(self, prompt=None):
        self.asked += 1
        return self.answers.pop(0) if self.answers else "n"


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


def _make(tmp_path, confirm_mode, human_io):
    from skill_engine.models import Skill, SkillMetadata, MatchResult
    from skill_engine.execution.executor import Executor
    from skill_engine.execution.assembler import Assembler
    from skill_engine.execution.tool_dispatch import ToolDispatchRunner

    skill = Skill(metadata=SkillMetadata(name="demo", description="d",
                                         confirm_edits=confirm_mode),
                  body="", directory=str(tmp_path))
    ex = Executor(timeout=10, allow_all=True)
    runner = ToolDispatchRunner(executor=ex, assembler=Assembler(executor=ex),
                                human_io=human_io, working_root=str(tmp_path))
    match = MatchResult(skill=skill, score=1.0, method="name", arguments={})
    return runner, match


def _tool_msgs(result, name):
    return [m.get("content", "") for m in result.get("history", [])
            if m.get("role") == "tool" and m.get("name") == name]


class TestConfirmTrue:
    def test_reject_keeps_file_unchanged(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        hio = _FakeHumanIO(["n"])
        runner, match = _make(tmp_path, "true", hio)
        llm = _ScriptedLLM([_step(1, "edit_file", {"path": "a.py",
                                  "edits": [{"oldText": "x = 1", "newText": "x = 2"}]})])
        result = runner.run(match, llm, max_iterations=4)
        assert f.read_text(encoding="utf-8") == "x = 1\n"
        assert any("用户拒绝" in m for m in _tool_msgs(result, "edit_file"))
        assert hio.asked == 1
        # diff 预览确实展示给了用户
        assert any("-x = 1" in e and "+x = 2" in e for e in hio.emitted)

    def test_approve_applies_edit(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        hio = _FakeHumanIO(["y"])
        runner, match = _make(tmp_path, "true", hio)
        llm = _ScriptedLLM([_step(1, "edit_file", {"path": "a.py",
                                  "edits": [{"oldText": "x = 1", "newText": "x = 2"}]})])
        runner.run(match, llm, max_iterations=4)
        assert f.read_text(encoding="utf-8") == "x = 2\n"

    def test_asks_every_time(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        hio = _FakeHumanIO(["y", "y"])
        runner, match = _make(tmp_path, "true", hio)
        llm = _ScriptedLLM([
            _step(1, "edit_file", {"path": "a.py",
                   "edits": [{"oldText": "x = 1", "newText": "x = 2"}]}),
            _step(2, "edit_file", {"path": "a.py",
                   "edits": [{"oldText": "x = 2", "newText": "x = 3"}]}),
        ])
        runner.run(match, llm, max_iterations=6)
        assert hio.asked == 2  # true 模式每次都问
        assert f.read_text(encoding="utf-8") == "x = 3\n"


class TestConfirmBatch:
    def test_remember_file_after_first_approval(self, tmp_path):
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("x = 1\n", encoding="utf-8")
        b.write_text("y = 1\n", encoding="utf-8")
        hio = _FakeHumanIO(["y", "y"])
        runner, match = _make(tmp_path, "batch", hio)
        llm = _ScriptedLLM([
            _step(1, "edit_file", {"path": "a.py",
                   "edits": [{"oldText": "x = 1", "newText": "x = 2"}]}),
            _step(2, "edit_file", {"path": "a.py",
                   "edits": [{"oldText": "x = 2", "newText": "x = 3"}]}),  # 同文件 → 自动放行
            _step(3, "edit_file", {"path": "b.py",
                   "edits": [{"oldText": "y = 1", "newText": "y = 2"}]}),  # 换文件 → 再问
        ])
        runner.run(match, llm, max_iterations=8)
        assert hio.asked == 2
        assert a.read_text(encoding="utf-8") == "x = 3\n"
        assert b.read_text(encoding="utf-8") == "y = 2\n"


class TestDefaultOff:
    def test_no_prompt_by_default(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        hio = _FakeHumanIO([])  # 无答案可读：若被询问会暴露
        runner, match = _make(tmp_path, "", hio)
        llm = _ScriptedLLM([_step(1, "edit_file", {"path": "a.py",
                                  "edits": [{"oldText": "x = 1", "newText": "x = 2"}]})])
        runner.run(match, llm, max_iterations=4)
        assert hio.asked == 0  # 默认关闭：不询问
        assert f.read_text(encoding="utf-8") == "x = 2\n"


class TestNoHumanIO:
    def test_degrades_to_display_only(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        runner, match = _make(tmp_path, "true", None)  # 无 human_io
        llm = _ScriptedLLM([_step(1, "edit_file", {"path": "a.py",
                                  "edits": [{"oldText": "x = 1", "newText": "x = 2"}]})])
        runner.run(match, llm, max_iterations=4)
        assert f.read_text(encoding="utf-8") == "x = 2\n"  # 降级放行


class TestWriteFileConfirm:
    def test_reject_overwrite(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("original\n", encoding="utf-8")
        hio = _FakeHumanIO(["n"])
        runner, match = _make(tmp_path, "true", hio)
        llm = _ScriptedLLM([_step(1, "write_file", {"path": "a.py", "content": "overwritten\n"})])
        result = runner.run(match, llm, max_iterations=4)
        assert f.read_text(encoding="utf-8") == "original\n"
        assert any("用户拒绝" in m for m in _tool_msgs(result, "write_file"))

    def test_new_file_shows_full_addition(self, tmp_path):
        hio = _FakeHumanIO(["y"])
        runner, match = _make(tmp_path, "true", hio)
        llm = _ScriptedLLM([_step(1, "write_file", {"path": "n.py", "content": "hello\n"})])
        runner.run(match, llm, max_iterations=4)
        assert (tmp_path / "n.py").read_text(encoding="utf-8") == "hello\n"
        assert any("+hello" in e for e in hio.emitted)
