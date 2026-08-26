"""
search_files 双实现测试（P0 S0-2）

覆盖：
- _python_search 回退实现：匹配 / glob 过滤 / max_results 截断提示 / 跳点目录
- _run_ripgrep：尊重 .gitignore、截断提示（rg 缺失时跳过）
- _search_files 入口：rg 不可用时自动回退
- 集成：ToolDispatchRunner 的 search_files 调用走双实现
"""

import shutil
import pytest
from pathlib import Path

from skill_engine.execution.tool_dispatch import (
    _python_search, _run_ripgrep, _search_files,
)


def _mk_project(tmp_path):
    (tmp_path / "kept.py").write_text("hello_target = 1\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("hello_target too\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.md").write_text("hello_target here\n", encoding="utf-8")
    (tmp_path / ".hiddendir").mkdir()
    (tmp_path / ".hiddendir" / "x.py").write_text("hello_target hidden\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    return tmp_path


class TestPythonSearchFallback:
    def test_basic_match(self, tmp_path):
        _mk_project(tmp_path)
        out = _python_search("hello_target", tmp_path, "", 100)
        assert "kept.py:1" in out
        # 纯 Python 实现跳过点目录
        assert "hiddendir" not in out

    def test_glob_filter(self, tmp_path):
        _mk_project(tmp_path)
        out = _python_search("hello_target", tmp_path, "*.md", 100)
        assert "deep.md" in out
        assert "kept.py" not in out

    def test_max_results_truncation_note(self, tmp_path):
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("needle\n", encoding="utf-8")
        out = _python_search("needle", tmp_path, "", 2)
        assert out.count("needle") >= 2
        assert "截断" in out  # 有截断提示

    def test_no_match(self, tmp_path):
        (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
        assert _python_search("zzz_not_there", tmp_path, "", 100) == "no matches found"


@pytest.mark.skipif(shutil.which("rg") is None, reason="本机无 ripgrep")
class TestRipgrepSearch:
    def test_respects_gitignore(self, tmp_path):
        _mk_project(tmp_path)
        out = _run_ripgrep("hello_target", tmp_path, "", 100)
        assert out is not None
        assert "kept.py" in out
        assert "ignored.txt" not in out  # .gitignore 生效

    def test_truncation_note_with_total(self, tmp_path):
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("needle\n", encoding="utf-8")
        out = _run_ripgrep("needle", tmp_path, "", 2)
        assert "共 5 条" in out

    def test_no_match(self, tmp_path):
        (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
        assert _run_ripgrep("zzz_not_there", tmp_path, "", 100) == "no matches found"


class TestSearchEntry:
    def test_fallback_when_rg_missing(self, tmp_path, monkeypatch):
        _mk_project(tmp_path)
        # 实现已搬迁至 tool_exec/search.py，patch 目标随实现走（tool_dispatch 仅 re-export）
        import skill_engine.execution.tool_exec.search as ts
        monkeypatch.setattr(ts.shutil, "which", lambda name: None)
        out = _search_files("hello_target", tmp_path)
        assert "kept.py" in out

    def test_default_max_applied(self, tmp_path, monkeypatch):
        import skill_engine.execution.tool_exec.search as ts
        captured = {}

        def fake_py(pattern, search_dir, file_glob, max_results, context_lines):
            captured["max"] = max_results
            return "no matches found"

        monkeypatch.setattr(ts, "_run_ripgrep", lambda *a, **k: None)
        monkeypatch.setattr(ts, "_python_search", fake_py)
        _search_files("x", tmp_path)
        assert captured["max"] == ts._SEARCH_DEFAULT_MAX


# ---------------- 集成：dispatch 循环内调用 ----------------
class _ScriptedLLM:
    def __init__(self, steps):
        self.steps = list(steps)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if self.steps:
            return self.steps.pop(0)
        return {"content": "完成", "tool_calls": []}


class TestDispatchIntegration:
    def test_search_via_dispatch(self, tmp_path):
        _mk_project(tmp_path)
        from skill_engine.models import Skill, SkillMetadata, MatchResult
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.tool_dispatch import ToolDispatchRunner

        skill = Skill(metadata=SkillMetadata(name="demo", description="d"),
                      body="", directory=str(tmp_path))
        ex = Executor(timeout=10, allow_all=True)
        runner = ToolDispatchRunner(executor=ex, assembler=Assembler(executor=ex),
                                    working_root=str(tmp_path))
        llm = _ScriptedLLM([
            {"content": "", "tool_calls": [
                {"id": "c1", "type": "search_files",
                 "input": {"pattern": "hello_target", "path": "."}}]},
        ])
        result = runner.run(MatchResult(skill=skill, score=1.0, method="name",
                                        arguments={}), llm, max_iterations=4)
        tool_msgs = [m.get("content", "") for m in result.get("history", [])
                     if m.get("role") == "tool" and m.get("name") == "search_files"]
        assert len(tool_msgs) == 1
        assert "kept.py" in tool_msgs[0]
