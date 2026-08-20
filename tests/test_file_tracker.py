"""
文件状态跟踪测试（P0 S0-1：read-before-write 一致性机制）

覆盖：
- FileStateTracker 单元行为：登记 / 陈旧检测 / bash 失效 / 软约束 vs 硬约束
- 集成：ToolDispatchRunner 的 read_file 登记、edit_file 软/硬约束、bash 后失效

测试策略与 test_large_code_p2.py / test_tool_pluggable.py 一致：
tmp_path + Scripted Mock LLM；bash 用例把安全模式设为 off（monkeypatch 环境变量）。
"""

import pytest
from pathlib import Path

from skill_engine.execution.file_tracker import FileStateTracker


# ---------------- 单元：FileStateTracker ----------------
class TestFileStateTracker:
    def test_unregistered_soft_warns_not_blocks(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1", encoding="utf-8")
        t = FileStateTracker(strict=False)
        ok, msg = t.check_editable(f)
        assert ok            # 软约束：不阻断
        assert msg           # 但带提示
        assert "read_file" in msg

    def test_unregistered_strict_blocks(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1", encoding="utf-8")
        t = FileStateTracker(strict=True)
        ok, msg = t.check_editable(f)
        assert not ok        # 硬约束：拒绝
        assert "read_file" in msg

    def test_read_then_check_passes(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1", encoding="utf-8")
        t = FileStateTracker(strict=True)
        t.on_read(f)
        ok, msg = t.check_editable(f)
        assert ok and msg == ""

    def test_external_change_detected_soft_and_strict(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("v1", encoding="utf-8")

        t_soft = FileStateTracker(strict=False)
        t_soft.on_read(f)
        f.write_text("v2-changed-content", encoding="utf-8")  # 尺寸/时间都变
        ok, msg = t_soft.check_editable(f)
        assert ok and msg    # 软：提示不阻断

        t_strict = FileStateTracker(strict=True)
        t_strict.on_read(f)
        f.write_text("v3-even-longer-content", encoding="utf-8")
        ok, msg = t_strict.check_editable(f)
        assert not ok        # 硬：拒绝

    def test_own_write_refreshes_registration(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("v1", encoding="utf-8")
        t = FileStateTracker(strict=True)
        t.on_read(f)
        f.write_text("v2", encoding="utf-8")     # 模拟 write_file/edit_file 写盘
        t.on_write(f)
        ok, msg = t.check_editable(f)
        assert ok and msg == ""

    def test_invalidate_all(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("v1", encoding="utf-8")
        t = FileStateTracker(strict=False)
        t.on_read(f)
        t.invalidate_all()
        ok, msg = t.check_editable(f)
        assert msg           # 回到"未登记"状态 → 重新提示
        assert "read_file" in msg

    def test_deleted_after_register_no_false_alarm(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("v1", encoding="utf-8")
        t = FileStateTracker(strict=True)
        t.on_read(f)
        f.unlink()
        ok, msg = t.check_editable(f)
        # 文件不存在交给 edit_file 自己的存在性检查，tracker 不误伤
        assert ok

    def test_invalidate_single_file_keeps_others(self, tmp_path):
        """选择性失效（性能诊断建议 6）：只失效命中的文件，其余登记保留。"""
        a, b = tmp_path / "a.py", tmp_path / "b.py"
        a.write_text("v1", encoding="utf-8")
        b.write_text("v1", encoding="utf-8")
        t = FileStateTracker(strict=True)
        t.on_read(a)
        t.on_read(b)
        t.invalidate_paths([a])
        ok_a, msg_a = t.check_editable(a)
        assert not ok_a and "read_file" in msg_a   # a 回到未登记
        ok_b, msg_b = t.check_editable(b)
        assert ok_b and msg_b == ""                 # b 仍已登记

    def test_invalidate_directory_covers_subtree(self, tmp_path):
        """目录 token → 该目录下所有登记一并失效（含 read 缓存）。"""
        (tmp_path / "pkg").mkdir()
        x = tmp_path / "pkg" / "x.py"
        y = tmp_path / "pkg" / "y.py"
        z = tmp_path / "other.py"
        for f in (x, y, z):
            f.write_text("v1", encoding="utf-8")
        t = FileStateTracker(strict=True)
        for f in (x, y, z):
            t.on_read(f)
            t.cache_read(f, 0, 0, 1, "content")
        t.invalidate_paths([tmp_path / "pkg"])
        for f in (x, y):
            ok, msg = t.check_editable(f)
            assert not ok and "read_file" in msg   # 目录下全部失效
            assert t.cache_lookup(f, 0, 0) is None  # read 缓存一并清
        ok_z, msg_z = t.check_editable(z)
        assert ok_z and msg_z == ""                 # 目录外不受影响

    def test_invalidate_nonexistent_path_noop(self, tmp_path):
        """失效一个从未登记的路径：无副作用、不抛异常。"""
        a = tmp_path / "a.py"
        a.write_text("v1", encoding="utf-8")
        t = FileStateTracker(strict=False)
        t.on_read(a)
        t.invalidate_paths([tmp_path / "ghost.py"])   # 从未读过的文件
        ok, msg = t.check_editable(a)
        assert ok and msg == ""


class TestExtractCmdPaths:
    """bash 命令路径提取（性能诊断建议 6 的前置解析）。"""

    def _paths(self, cmd, tmp_path):
        from skill_engine.execution.tool_dispatch import _extract_cmd_paths
        return _extract_cmd_paths(cmd, tmp_path)

    def test_no_path_token_returns_none(self, tmp_path):
        """echo hi / git status 无路径 token → None → 调用方保守全失效。"""
        assert self._paths("echo hi", tmp_path) is None
        assert self._paths("git status", tmp_path) is None
        assert self._paths("pytest -k smoke", tmp_path) is None

    def test_quoted_ordinary_string_kept_inside_root(self, tmp_path):
        """引号包裹的字符串保留为候选路径（echo "x" > "out.txt" 重定向靠它命中），
        但被钳在工作目录内，且多为未登记路径 → 失效是 no-op。"""
        p = self._paths('echo "hello"', tmp_path)
        assert p == [(tmp_path / "hello").resolve()]

    def test_relative_path_with_separator(self, tmp_path):
        (tmp_path / "src").mkdir()
        p = self._paths("python src/build.py", tmp_path)
        assert p == [(tmp_path / "src" / "build.py").resolve()]

    def test_dir_token_returns_dir(self, tmp_path):
        (tmp_path / "tests").mkdir()
        p = self._paths("rm -rf tests/", tmp_path)
        assert p == [(tmp_path / "tests").resolve()]

    def test_outside_working_root_ignored(self, tmp_path):
        """盘符绝对路径/.. 逃逸：即使被分词成片段，最终失效路径也绝不出工作目录。"""
        root = tmp_path.resolve()
        for cmd in ("rm -rf C:/Windows/Temp/x", "rm -rf ../../etc/hosts"):
            p = self._paths(cmd, tmp_path)
            for path in p:
                path.relative_to(root)   # 抛 ValueError = 逃逸出工作目录，测试即失败

    def test_bare_filename_conservative_none(self, tmp_path):
        """touch newfile.txt：裸文件名（无分隔符）解析不到 → None → 保守全失效。"""
        target = tmp_path / "newfile.txt"
        target.write_text("", encoding="utf-8")
        assert self._paths("touch newfile.txt", tmp_path) is None


# ---------------- 集成：Scripted Mock LLM ----------------
class _ScriptedLLM:
    """按脚本逐轮返回 tool_calls 的 Mock LLM（脚本耗尽后以文本收尾）。"""

    def __init__(self, steps):
        self.steps = list(steps)
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        if self.steps:
            return self.steps.pop(0)
        return {"content": "完成", "tool_calls": []}


def _tool_msgs(result, name):
    """从 run 历史里取指定工具名的 tool 消息内容列表。"""
    return [m.get("content", "") for m in result.get("history", [])
            if m.get("role") == "tool" and m.get("name") == name]


def _make_skill(tmp_path, strict: bool):
    from skill_engine.models import Skill, SkillMetadata
    return Skill(
        metadata=SkillMetadata(name="demo", description="d",
                               strict_file_tracking=strict),
        body="", directory=str(tmp_path),
    )


def _make_runner(tmp_path):
    from skill_engine.execution.executor import Executor
    from skill_engine.execution.assembler import Assembler
    from skill_engine.execution.tool_dispatch import ToolDispatchRunner
    ex = Executor(timeout=10, allow_all=True)
    return ToolDispatchRunner(executor=ex, assembler=Assembler(executor=ex),
                              working_root=str(tmp_path))


def _step(i, tool, args):
    return {"content": "", "tool_calls": [
        {"id": f"c{i}", "type": tool, "input": args}]}


class TestEditFileTracking:
    def test_soft_mode_edit_without_read_applies_with_hint(self, tmp_path):
        from skill_engine.models import MatchResult
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        skill = _make_skill(tmp_path, strict=False)
        llm = _ScriptedLLM([
            _step(1, "edit_file", {"path": "a.py",
                                   "edits": [{"oldText": "x = 1", "newText": "x = 2"}]}),
        ])
        result = _make_runner(tmp_path).run(
            MatchResult(skill=skill, score=1.0, method="name", arguments={}),
            llm, max_iterations=5)
        msgs = _tool_msgs(result, "edit_file")
        assert len(msgs) == 1
        assert "applied 1 edits" in msgs[0]      # 软约束：照样应用
        assert "read_file" in msgs[0]            # 但附提示
        assert f.read_text(encoding="utf-8") == "x = 2\n"

    def test_strict_mode_blocks_until_read(self, tmp_path):
        from skill_engine.models import MatchResult
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        skill = _make_skill(tmp_path, strict=True)
        llm = _ScriptedLLM([
            _step(1, "edit_file", {"path": "a.py",
                                   "edits": [{"oldText": "x = 1", "newText": "x = 2"}]}),
            _step(2, "read_file", {"path": "a.py"}),
            _step(3, "edit_file", {"path": "a.py",
                                   "edits": [{"oldText": "x = 1", "newText": "x = 2"}]}),
        ])
        result = _make_runner(tmp_path).run(
            MatchResult(skill=skill, score=1.0, method="name", arguments={}),
            llm, max_iterations=6)
        msgs = _tool_msgs(result, "edit_file")
        assert len(msgs) == 2
        assert "一致性校验未通过" in msgs[0]      # 第一次被拒
        assert "applied 1 edits" in msgs[1]      # 重读后通过
        assert f.read_text(encoding="utf-8") == "x = 2\n"

    def test_bash_invalidates_registration(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_SECURITY_MODE", "off")
        from skill_engine.models import MatchResult
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        skill = _make_skill(tmp_path, strict=False)
        llm = _ScriptedLLM([
            _step(1, "read_file", {"path": "a.py"}),
            _step(2, "bash", {"command": "echo hi"}),
            _step(3, "edit_file", {"path": "a.py",
                                   "edits": [{"oldText": "x = 1", "newText": "x = 2"}]}),
        ])
        result = _make_runner(tmp_path).run(
            MatchResult(skill=skill, score=1.0, method="name", arguments={}),
            llm, max_iterations=6)
        msgs = _tool_msgs(result, "edit_file")
        assert len(msgs) == 1
        assert "applied 1 edits" in msgs[0]
        # bash 后登记失效 → 软约束重新提示重读
        assert "read_file" in msgs[0]

    def test_write_then_edit_no_warning(self, tmp_path):
        from skill_engine.models import MatchResult
        skill = _make_skill(tmp_path, strict=False)
        llm = _ScriptedLLM([
            _step(1, "write_file", {"path": "n.py", "content": "y = 1\n"}),
            _step(2, "edit_file", {"path": "n.py",
                                   "edits": [{"oldText": "y = 1", "newText": "y = 2"}]}),
        ])
        result = _make_runner(tmp_path).run(
            MatchResult(skill=skill, score=1.0, method="name", arguments={}),
            llm, max_iterations=5)
        msgs = _tool_msgs(result, "edit_file")
        assert len(msgs) == 1
        assert "applied 1 edits" in msgs[0]
        assert "提示" not in msgs[0]             # 自己写过的文件：无提示

    def test_bash_selective_invalidation_only_touched_file(self, tmp_path, monkeypatch):
        """性能诊断建议 6：bash 命令带明确路径 → 只失效该文件，其余登记保留。"""
        monkeypatch.setenv("SKILLS_ENGINE_SECURITY_MODE", "off")
        from skill_engine.models import MatchResult
        src = tmp_path / "src"
        src.mkdir()
        a = src / "a.py"
        b = src / "b.py"
        a.write_text("x = 1\n", encoding="utf-8")
        b.write_text("y = 1\n", encoding="utf-8")
        skill = _make_skill(tmp_path, strict=True)
        llm = _ScriptedLLM([
            _step(1, "read_file", {"path": "src/a.py"}),
            _step(2, "read_file", {"path": "src/b.py"}),
            _step(3, "bash", {"command": "python src/a.py"}),   # 只动 a.py
            _step(4, "edit_file", {"path": "src/b.py",
                                   "edits": [{"oldText": "y = 1", "newText": "y = 2"}]}),
            _step(5, "edit_file", {"path": "src/a.py",
                                   "edits": [{"oldText": "x = 1", "newText": "x = 2"}]}),
        ])
        result = _make_runner(tmp_path).run(
            MatchResult(skill=skill, score=1.0, method="name", arguments={}),
            llm, max_iterations=8)
        msgs = _tool_msgs(result, "edit_file")
        # 第 1 次编辑 b.py：bash 未碰它 → 登记保留，直接通过且无提示
        assert "applied 1 edits" in msgs[0] and "提示" not in msgs[0]
        # 第 2 次编辑 a.py：bash 碰过它 → 已失效，strict 硬约束要求重读
        assert "一致性校验未通过" in msgs[1]
        assert b.read_text(encoding="utf-8") == "y = 2\n"
        assert a.read_text(encoding="utf-8") == "x = 1\n"
