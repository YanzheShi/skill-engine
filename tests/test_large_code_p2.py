"""
P2 能力测试（大型代码处理 — 可靠层）

覆盖：
- P2-1：edit_file 模糊兜底（_apply_edits 精确优先 + 行级宽松模糊匹配）
- P2-2：通用文件快照/回滚（FileSnapshot + 内置 restore_file 工具，不依赖 git）
- P2-3：todo 落盘续跑（run 的 state_path / resume_from，见文件末尾 TestResume）

测试策略：
- _apply_edits 为纯函数，零文件 IO，最稳健。
- FileSnapshot / 集成测使用 tmp_path 临时目录（与 test_tool_pluggable.py 一致）。
"""

import json
import os
import pytest
from pathlib import Path
from skill_engine.execution.tool_dispatch import _apply_edits
from skill_engine.execution.snapshot import FileSnapshot


# ---------------- P2-1：_apply_edits 纯函数 ----------------
class TestApplyEdits:
    def test_exact_single(self):
        content = "line1\nline2\nline3\n"
        edits = [{"oldText": "line2", "newText": "LINE2"}]
        new, err = _apply_edits(content, edits)
        assert err is None
        assert new == "line1\nLINE2\nline3\n"

    def test_exact_multiple_sorted_by_position(self):
        content = "a\nb\nc\nd\n"
        edits = [
            {"oldText": "c", "newText": "C"},
            {"oldText": "b", "newText": "B"},
        ]
        new, err = _apply_edits(content, edits)
        assert err is None
        assert new == "a\nB\nC\nd\n"

    def test_oldtext_missing_then_fuzzy_whitespace(self):
        # LLM 给的 oldText 缩进多了一层（常见错误），精确失败 → 行级 strip 模糊命中
        content = "class X:\n    def foo():\n        return 1\n"
        edits = [{"oldText": "        def foo():\n        return 1",
                  "newText": "    def foo():\n        return 2"}]
        new, err = _apply_edits(content, edits)
        assert err is None, err
        assert "return 2" in new
        assert "class X:" in new

    def test_fuzzy_normalize_inner_ws(self):
        # 行内多空白归一化
        content = "x =    1\ny = 2\n"
        edits = [{"oldText": "x = 1", "newText": "x = 99"}]
        new, err = _apply_edits(content, edits)
        assert err is None, err
        assert "x = 99" in new

    def test_exact_duplicate_oldtext_errors(self):
        content = "a\nb\na\n"
        edits = [{"oldText": "a", "newText": "A"}]
        new, err = _apply_edits(content, edits)
        assert new is None
        assert "出现 2 次" in err

    def test_fuzzy_also_fails(self):
        content = "hello world\n"
        edits = [{"oldText": "nonexistent text", "newText": "x"}]
        new, err = _apply_edits(content, edits)
        assert new is None
        assert "不存在" in err

    def test_empty_edits(self):
        new, err = _apply_edits("abc", [])
        assert new is None and "edits 列表为空" in err

    def test_missing_oldtext(self):
        new, err = _apply_edits("abc", [{"newText": "x"}])
        assert new is None and "缺少 oldText" in err

    def test_fuzzy_multiple_candidates_fails(self):
        # 精确不存在（LLM 漏了前导空格），但模糊 strip 后命中多处 → 无法唯一 → 报错
        content = "  a\n  b\n  a\n  b\n"
        edits = [{"oldText": "a\nb", "newText": "X\nY"}]
        new, err = _apply_edits(content, edits)
        assert new is None
        assert "不存在" in err


# ---------------- P2-2：FileSnapshot ----------------
class TestFileSnapshot:
    def test_record_then_restore(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("original", encoding="utf-8")
        snap = FileSnapshot(tmp_path)
        snap.record(f, "original")
        f.write_text("modified", encoding="utf-8")
        ok, msg = snap.restore(f)
        assert ok
        assert f.read_text(encoding="utf-8") == "original"

    def test_record_only_first(self, tmp_path):
        # 仅首次记录"进入前状态"，后续覆盖不应更新检查点
        f = tmp_path / "f.txt"
        f.write_text("v1", encoding="utf-8")
        snap = FileSnapshot(tmp_path)
        snap.record(f, "v1")
        f.write_text("v2", encoding="utf-8")
        snap.record(f, "v2")
        ok, msg = snap.restore(f)
        assert ok
        assert f.read_text(encoding="utf-8") == "v1"

    def test_restore_no_snapshot(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x", encoding="utf-8")
        snap = FileSnapshot(tmp_path)
        ok, msg = snap.restore(f)
        assert not ok


# ---------------- P2-1 + P2-2 集成：edit 模糊 + restore 回滚 ----------------
class _MockEditRestoreLLM:
    """第一轮 edit（触发模糊），第二轮 restore，第三轮收尾。"""

    def __init__(self):
        self.call_count = 0

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        self.call_count += 1
        if self.call_count == 1:
            return {"content": "", "tool_calls": [
                {"id": "e1", "type": "edit_file", "input": {
                    "path": "target.py",
                    "edits": [{"oldText": "        def bar():\n        pass",
                               "newText": "    def bar():\n        return 42"}]
                }}
            ]}
        if self.call_count == 2:
            return {"content": "", "tool_calls": [
                {"id": "r1", "type": "restore_file", "input": {"path": "target.py"}}
            ]}
        return {"content": "done", "tool_calls": []}


class TestEditAndRestoreIntegration:
    def test_edit_fuzzy_and_restore(self, tmp_path):
        from skill_engine.models import Skill, SkillMetadata, MatchResult
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner

        original = "class X:\n    def bar():\n        pass\n"
        (tmp_path / "target.py").write_text(original, encoding="utf-8")

        skill = Skill(
            metadata=SkillMetadata(name="code-builder", description="d"),
            body="", directory=str(tmp_path),
        )
        match = MatchResult(skill=skill, score=1.0, method="name", arguments={})
        runner = Runner(Assembler(executor=Executor(timeout=10)), Executor(timeout=10))
        llm = _MockEditRestoreLLM()
        result = runner.run(match, tool_dispatch=llm, max_iterations=5)

        target = tmp_path / "target.py"
        # restore 后应回到进入前的 original 状态
        assert target.read_text(encoding="utf-8") == original
        assert result["stopped_by"] == "stop"
        hist = str(result.get("history", []))
        assert "未知工具类型" not in hist

    def test_edit_fuzzy_was_applied_before_restore(self, tmp_path):
        """确认 edit 真的改了文件（模糊命中），restore 才把改动撤掉。"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner

        original = "class X:\n    def bar():\n        pass\n"
        (tmp_path / "target.py").write_text(original, encoding="utf-8")

        skill = Skill(
            metadata=SkillMetadata(name="code-builder", description="d"),
            body="", directory=str(tmp_path),
        )
        match = MatchResult(skill=skill, score=1.0, method="name", arguments={})

        # 只做 edit、不 restore
        class _OnlyEdit:
            def __init__(self): self.n = 0
            def bind_tools(self, tools): return self
            def invoke(self, messages):
                self.n += 1
                if self.n == 1:
                    return {"content": "", "tool_calls": [
                        {"id": "e1", "type": "edit_file", "input": {
                            "path": "target.py",
                            "edits": [{"oldText": "        def bar():\n        pass",
                                       "newText": "    def bar():\n        return 42"}]
                        }}
                    ]}
                return {"content": "done", "tool_calls": []}

        runner = Runner(Assembler(executor=Executor(timeout=10)), Executor(timeout=10))
        runner.run(match, tool_dispatch=_OnlyEdit(), max_iterations=5)
        edited = tmp_path / "target.py"
        assert "return 42" in edited.read_text(encoding="utf-8")
        assert "pass" not in edited.read_text(encoding="utf-8")


# ---------------- P2-3：todo 落盘续跑 ----------------
class _WriteThenStop:
    """第一轮 write_file 写文件，第二轮收尾。"""
    def __init__(self): self.n = 0
    def bind_tools(self, tools): return self
    def invoke(self, messages):
        self.n += 1
        if self.n == 1:
            return {"content": "", "tool_calls": [
                {"id": "w1", "type": "write_file",
                 "input": {"path": "a.txt", "content": "hello"}}
            ]}
        return {"content": "done", "tool_calls": []}


class _ResumeOnly:
    """续跑：直接收尾，不调用任何工具。"""
    def bind_tools(self, tools): return self
    def invoke(self, messages):
        return {"content": "resumed", "tool_calls": []}


def _make_match(tmp_path):
    from skill_engine.models import Skill, SkillMetadata, MatchResult
    skill = Skill(
        metadata=SkillMetadata(name="code-builder", description="d"),
        body="", directory=str(tmp_path),
    )
    return MatchResult(skill=skill, score=1.0, method="name", arguments={})


class TestResume:
    def test_state_persisted(self, tmp_path):
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner

        state_file = tmp_path / "run_state.json"
        runner = Runner(Assembler(executor=Executor(timeout=10)), Executor(timeout=10))
        runner.run(_make_match(tmp_path), tool_dispatch=_WriteThenStop(),
                   max_iterations=5, state_path=str(state_file))

        assert state_file.exists(), "state_path 未落盘状态文件"
        # JSONL（append-only）：取最后一行的完整快照
        lines = [ln for ln in state_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert lines, "状态文件为空"
        st = json.loads(lines[-1])
        assert "messages" in st and len(st["messages"]) >= 3
        assert "final_prompt" in st
        # messages 含 user / assistant(write_file) / tool 三类
        roles = {m.get("role") for m in st["messages"]}
        assert {"user", "assistant", "tool"}.issubset(roles)

    def test_resume_continues_conversation(self, tmp_path):
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner

        state_file = tmp_path / "run_state.json"
        runner = Runner(Assembler(executor=Executor(timeout=10)), Executor(timeout=10))

        # 第一轮：写文件并停止，落盘
        runner.run(_make_match(tmp_path), tool_dispatch=_WriteThenStop(),
                   max_iterations=5, state_path=str(state_file))
        # 第二轮：从状态续跑，LLM 直接收尾（不再写文件）
        result = runner.run(_make_match(tmp_path), tool_dispatch=_ResumeOnly(),
                            max_iterations=5, resume_from=str(state_file))

        # 续跑的 history 必须包含上一轮 write_file 的工具消息（证明载入而非重跑）
        hist = result.get("history", [])
        tool_msgs = [m for m in hist if m.get("role") == "tool"]
        assert any(m.get("name") == "write_file" for m in tool_msgs), \
            "resume 未载入上次的 write_file 工具消息"
        assert any("wrote" in (m.get("content") or "") and "a.txt" in (m.get("content") or "")
                   for m in tool_msgs), "续跑历史缺少上一轮写文件结果"
        # 文件未被二次写入（resume LLM 没调用 write_file）
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"
