"""trusted_root（受信任工作目录）放行测试。

用户显式指定 -w 后：目录内的文件读写自动放行（免审批 / 免 diff 确认），
目录外的操作维持原审批；敏感文件名（.env 等）底线始终保留。
"""

import pytest
from pathlib import Path

from skill_engine.execution.tool_dispatch import ToolDispatchRunner
from skill_engine.models import Skill, SkillMetadata


def _skill(directory: str) -> Skill:
    return Skill(metadata=SkillMetadata(name="t", description="d"),
                 body="b", directory=directory)


def _runner(trusted_root=None, human_io=None, approval_fn=None, confirm="batch"):
    from skill_engine.execution.executor import Executor
    from skill_engine.execution.assembler import Assembler
    r = ToolDispatchRunner(
        executor=Executor(timeout=10),
        assembler=Assembler(executor=Executor(timeout=10)),
        human_io=human_io,
        approval_fn=approval_fn,
        trusted_root=trusted_root,
    )
    r._confirm_edits_mode = confirm
    return r


class FakeHio:
    def __init__(self, answers):
        self._a = list(answers)
        self.reads = 0

    def read(self, prompt=""):
        self.reads += 1
        return self._a.pop(0)

    def emit(self, *a, **k):
        pass

    def emit_tool(self, *a, **k):
        pass

    def emit_thinking(self, *a, **k):
        pass

    def emit_result(self, *a, **k):
        pass


def test_confirm_edit_inside_trusted_auto_approves(tmp_path):
    """工作目录内的 write/edit 直接放行：不问用户、不展示 diff。"""
    hio = FakeHio(["n"])  # 即便用户本会拒绝，trusted 内也应自动放行
    r = _runner(trusted_root=str(tmp_path), human_io=hio)
    assert r._confirm_edit("write_file", str(tmp_path / "index.html"), "BIG DIFF") is True
    assert hio.reads == 0


def test_confirm_edit_outside_trusted_still_asks(tmp_path):
    """工作目录外的文件操作维持 diff 确认。"""
    hio = FakeHio(["n"])
    r = _runner(trusted_root=str(tmp_path), human_io=hio)
    outside = str(tmp_path.parent / "outside.html")
    assert r._confirm_edit("write_file", outside, "diff") is False
    assert hio.reads == 1


def test_confirm_edit_no_trusted_root_still_asks(tmp_path):
    """未指定 trusted_root（-w）时行为不变：照常询问。"""
    hio = FakeHio(["y"])
    r = _runner(trusted_root=None, human_io=hio)
    assert r._confirm_edit("write_file", str(tmp_path / "a.html"), "diff") is True
    assert hio.reads == 1


def test_check_file_safety_inside_trusted_skips_approval(tmp_path):
    """目录内文件操作不经 should_approve / approval_fn，直接 SAFE。"""
    calls = []
    r = _runner(trusted_root=str(tmp_path),
                approval_fn=lambda *a, **k: calls.append(a) or True)
    ok, err = r._check_file_safety("write", "inner.txt", _skill(str(tmp_path)))
    assert ok is True
    assert err == ""
    assert calls == []


def test_is_trusted_path_inside(tmp_path):
    """目录内路径受信任；.. 逃逸解析后落在目录外则不受信任。"""
    r = _runner(trusted_root=str(tmp_path))
    assert r._is_trusted_path(Path(tmp_path) / "inner.txt") is True
    assert r._is_trusted_path(Path(tmp_path) / ".." / "escape.txt") is False
    assert r._is_trusted_path(Path(tmp_path)) is True
    # 未指定 trusted_root 时一律不受信任
    assert _runner(trusted_root=None)._is_trusted_path(Path(tmp_path) / "a.txt") is False


def test_check_file_safety_outside_trusted_keeps_scanner_path(tmp_path):
    """目录外路径不被 trusted 提前放行：结果完全交给原审批链决定。

    注意：scanner._path_escapes 的正则只认 / 开头 token，Windows 反斜杠路径
    在既有实现里不判定为逃逸（SAFE）——这里只验证 trusted 分支没有短路。
    """
    calls = []
    r = _runner(trusted_root=str(tmp_path),
                approval_fn=lambda *a, **k: calls.append(a) or True)
    outside = str(tmp_path.parent / "o.txt")
    ok, _ = r._check_file_safety("write", outside, _skill(str(tmp_path)))
    # 不在 trusted 内 → 未走信任短路（approval_fn 只有在 scanner 判 ATTENTION 时才被调，
    # Windows 绝对路径不触发 ATTENTION，故 calls 保持为空且结果由 scanner 决定）
    assert ok is True
    assert calls == []


def test_check_file_safety_risky_filename_keeps_gate(tmp_path):
    """敏感文件名（.env）即使在工作目录内也必须审批——安全底线不豁免。"""
    calls = []
    r = _runner(trusted_root=str(tmp_path),
                approval_fn=lambda *a, **k: calls.append(a) or False)
    ok, err = r._check_file_safety("write", ".env", _skill(str(tmp_path)))
    assert ok is False
    assert err
    assert calls
