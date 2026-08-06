"""
bash 工具 timeout 参数测试（P0 S0-4 的前半）

覆盖：
- LLM 传 timeout → 透传给 Executor.run_step（spy 捕获）
- 超过硬上限 BASH_MAX_TIMEOUT 时被钳制
- 不传 timeout → 沿用 Executor 默认（None 透传）
- 真实超时行为：长命令按传入 timeout 被杀掉，observation 标 timed_out
"""

import time
import pytest

from skill_engine.execution.tool_dispatch import BASH_MAX_TIMEOUT


class _ScriptedLLM:
    def __init__(self, steps):
        self.steps = list(steps)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if self.steps:
            return self.steps.pop(0)
        return {"content": "完成", "tool_calls": []}


def _step(i, args):
    return {"content": "", "tool_calls": [
        {"id": f"c{i}", "type": "bash", "input": args}]}


def _make(tmp_path):
    from skill_engine.models import Skill, SkillMetadata, MatchResult
    from skill_engine.execution.executor import Executor
    from skill_engine.execution.assembler import Assembler
    from skill_engine.execution.tool_dispatch import ToolDispatchRunner
    skill = Skill(metadata=SkillMetadata(name="demo", description="d"),
                  body="", directory=str(tmp_path))
    ex = Executor(timeout=10, allow_all=True)
    runner = ToolDispatchRunner(executor=ex, assembler=Assembler(executor=ex),
                                working_root=str(tmp_path))
    match = MatchResult(skill=skill, score=1.0, method="name", arguments={})
    return runner, match, ex


class TestBashTimeoutParam:
    def test_timeout_forwarded_to_executor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_SECURITY_MODE", "off")
        runner, match, ex = _make(tmp_path)
        captured = {}

        def spy(command, cwd=None, allowlist_override=None, timeout=None):
            captured["timeout"] = timeout
            return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}

        monkeypatch.setattr(ex, "run_step", spy)
        llm = _ScriptedLLM([_step(1, {"command": "echo hi", "timeout": 120})])
        runner.run(match, llm, max_iterations=3)
        assert captured["timeout"] == 120

    def test_timeout_clamped_to_max(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_SECURITY_MODE", "off")
        runner, match, ex = _make(tmp_path)
        captured = {}

        def spy(command, cwd=None, allowlist_override=None, timeout=None):
            captured["timeout"] = timeout
            return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}

        monkeypatch.setattr(ex, "run_step", spy)
        llm = _ScriptedLLM([_step(1, {"command": "echo hi", "timeout": 999999})])
        runner.run(match, llm, max_iterations=3)
        assert captured["timeout"] == BASH_MAX_TIMEOUT

    def test_no_timeout_param_uses_engine_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_SECURITY_MODE", "off")
        runner, match, ex = _make(tmp_path)
        captured = {}

        def spy(command, cwd=None, allowlist_override=None, timeout=None):
            captured["timeout"] = timeout
            return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}

        monkeypatch.setattr(ex, "run_step", spy)
        llm = _ScriptedLLM([_step(1, {"command": "echo hi"})])
        runner.run(match, llm, max_iterations=3)
        assert captured["timeout"] is None       # 不覆盖 Executor 默认

    def test_invalid_timeout_value_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_SECURITY_MODE", "off")
        runner, match, ex = _make(tmp_path)
        captured = {}

        def spy(command, cwd=None, allowlist_override=None, timeout=None):
            captured["timeout"] = timeout
            return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}

        monkeypatch.setattr(ex, "run_step", spy)
        llm = _ScriptedLLM([_step(1, {"command": "echo hi", "timeout": "abc"})])
        runner.run(match, llm, max_iterations=3)
        assert captured["timeout"] is None

    def test_real_timeout_kills_long_command(self, tmp_path, monkeypatch):
        import sys
        monkeypatch.setenv("SKILLS_ENGINE_SECURITY_MODE", "off")
        runner, match, ex = _make(tmp_path)
        # 跨平台长命令：Windows 无 sleep 命令
        if sys.platform == "win32":
            long_cmd = "powershell -Command Start-Sleep -Seconds 5"
        else:
            long_cmd = "sleep 5"
        llm = _ScriptedLLM([_step(1, {"command": long_cmd, "timeout": 1})])
        t0 = time.monotonic()
        result = runner.run(match, llm, max_iterations=3)
        elapsed = time.monotonic() - t0
        # timeout 生效的核心判定：observation 标记 timed_out。
        # 注意：Windows 下被杀的 powershell 会遗留 conhost 子进程继续 sleep，
        # 导致 subprocess 回收耗时≈命令自然时长，故不依赖 elapsed 严格小于 timeout。
        tool_msgs = [m.get("content", "") for m in result.get("history", [])
                     if m.get("role") == "tool" and m.get("name") == "bash"]
        assert tool_msgs, "未记录 bash 工具调用"
        assert "timed_out" in tool_msgs[0]
        assert elapsed < 30, "命令未被超时回收，疑似挂起"
