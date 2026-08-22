"""DebugTracer + 执行链路落盘的最小测试。

覆盖：
1. DebugTracer 关闭时全程 no-op（不建文件、event/dump/close 都不抛）。
2. DebugTracer 开启时写 JSONL（每行合法 JSON、含 ts+kind），并单独 dump .ctx.json。
3. 行缓冲：未 close 也即时落盘，崩溃不丢。
4. CliHumanIO 经 set_tracer 把 emit*/read 输出落为事件。
5. ToolDispatchRunner._trace_finish 包装 RunResult 并记 stop 事件。
6. ToolDispatchRunner.run() 端到端：run_start/iteration/llm_response/stop/context 全记录。
"""

import json
from pathlib import Path

import pytest

from skill_engine.execution.tracer import DebugTracer, truncate
from skill_engine.execution.human_io import CliHumanIO
from skill_engine.execution.runner import Runner
from skill_engine.execution.tool_dispatch import ToolDispatchRunner
from skill_engine.models import Skill, SkillMetadata, MatchResult, RunResult


def test_tracer_disabled_is_noop():
    t = DebugTracer(None)
    assert t.enabled() is False
    assert t.log_path() is None
    t.event("x", a=1)            # 不应抛
    t.dump_context([], [], [])   # 不应抛
    t.close()                    # 不应抛


def test_tracer_writes_jsonl_and_ctx(tmp_path):
    log = str(tmp_path / "debug.log")
    t = DebugTracer(log)
    assert t.enabled()
    t.event("header", title="Running in x")
    t.event("iteration", n=1, max=3)
    t.dump_context(
        messages=[{"role": "user", "content": "hi"}],
        step_results=[{"name": "a"}],
        files_created=["f.py"],
        skill_name="x",
        iterations=1,
        stopped_by="stop",
    )
    t.close()

    lines = Path(log).read_text(encoding="utf-8").strip().splitlines()
    kinds = [json.loads(l)["kind"] for l in lines]
    assert "header" in kinds and "iteration" in kinds and "context" in kinds
    for l in lines:           # 每行都是合法 JSON
        json.loads(l)

    ctx_path = log + ".ctx.json"
    assert Path(ctx_path).exists()
    ctx = json.loads(Path(ctx_path).read_text(encoding="utf-8"))
    assert ctx["skill_name"] == "x"
    assert ctx["messages"][0]["content"] == "hi"


def test_tracer_line_buffered_before_close(tmp_path):
    log = str(tmp_path / "d2.log")
    t = DebugTracer(log)
    t.event("run_start")
    # 行缓冲：未 close 时事件已落盘
    assert "run_start" in Path(log).read_text(encoding="utf-8")
    t.close()


def test_cli_humanio_traces_emits(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    log = str(tmp_path / "h.log")
    t = DebugTracer(log)
    hio = CliHumanIO(paste_dir=str(tmp_path))
    hio.set_verbose(True)
    hio.set_tracer(t)

    hio.emit_header("Title")
    hio.emit("hello world", label="[AI] ")
    hio.emit_tool("bash", "ls -la")
    hio.emit_result("out line")
    hio.emit_thinking("I think therefore")
    hio.emit_command("echo hi")
    hio.read(prompt="you> ")

    t.close()
    kinds = [json.loads(l)["kind"] for l in Path(log).read_text(encoding="utf-8").strip().splitlines()]
    assert {"header", "emit", "tool", "result", "thinking", "command", "user_input"} <= set(kinds)


def test_trace_finish_records_stop(tmp_path):
    t = DebugTracer(str(tmp_path / "tf.log"))
    runner = ToolDispatchRunner.__new__(ToolDispatchRunner)
    runner.tracer = t
    res = RunResult(output="x", ctx={"stopped_by": "stop", "iterations": 2}, history=[])
    assert runner._trace_finish(res) is res
    t.close()
    kinds = [json.loads(l)["kind"] for l in Path(str(tmp_path / "tf.log")).read_text(encoding="utf-8").strip().splitlines()]
    assert "stop" in kinds


def test_tool_dispatch_run_records_debug_trace(tmp_path, monkeypatch):
    import skill_engine.execution.tool_dispatch as td_mod
    monkeypatch.setattr(td_mod, "load_skill_tools", lambda skill: [])
    monkeypatch.setattr(td_mod, "load_mcp_tools", lambda skill: [])
    monkeypatch.setattr(td_mod, "build_env_header", lambda base_dir, shell: "")

    log = str(tmp_path / "td.log")
    tracer = DebugTracer(log)

    class FakeExec:
        def run_step(self, cmd, cwd=None, timeout=None):
            return {"returncode": 0, "stdout": "ok", "stderr": ""}

    class FakeAsm:
        def assemble(self, skill, arguments, plain_text=False):
            return "do something"

    class FakeLLM:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            class R:
                content = "all done"
                tool_calls = []
                additional_kwargs = {}
            return R()

    meta = SkillMetadata(name="s", description="d")
    skill = Skill(metadata=meta, body="", directory=str(tmp_path))
    mr = MatchResult(skill=skill, score=1.0, method="name", arguments={})

    runner = ToolDispatchRunner(
        executor=FakeExec(), assembler=FakeAsm(), approval_fn=None,
        human_io=None, working_root=str(tmp_path), tracer=tracer,
    )
    res = runner.run(mr, FakeLLM(), max_iterations=3)
    tracer.close()

    lines = [json.loads(l) for l in Path(log).read_text(encoding="utf-8").strip().splitlines()]
    kinds = [l["kind"] for l in lines]
    assert {"run_start", "iteration", "llm_response", "stop", "context"} <= set(kinds)

    stop_ev = [l for l in lines if l["kind"] == "stop"][0]
    assert stop_ev["stopped_by"] == res.ctx["stopped_by"]

    ctx = json.loads(Path(log + ".ctx.json").read_text(encoding="utf-8"))
    assert isinstance(ctx["messages"], list) and len(ctx["messages"]) >= 1


def test_truncate_helper():
    assert truncate("abc", 10) == "abc"
    out = truncate("x" * 50, 10)
    assert len(out) > 10          # 截断说明后缀已追加
    assert "截断" in out
    # 未超长时不截断
    assert truncate("short", 100) == "short"


def test_run_normal_completion_after_tool_exec_no_crash(tmp_path, monkeypatch):
    """回归：主循环执行过 skill 工具（result 被覆盖成 str）后，finally 的
    dump_context 不再崩。

    原 bug：run() 的 finally 里引用局部变量 ``result.ctx``，但主循环中
    ``result = tool_obj.invoke(...)`` 把它赋成工具输出字符串，正常完成路径
    （不经过 max_iterations 的 RunResult 赋值）下抛
    AttributeError: 'str' object has no attribute 'ctx'。
    """
    import skill_engine.execution.tool_dispatch as td_mod
    monkeypatch.setattr(td_mod, "load_mcp_tools", lambda skill: [])
    monkeypatch.setattr(td_mod, "build_env_header", lambda base_dir, shell: "")

    class FakeTool:
        name = "my_tool"

        def invoke(self, inp):
            return "some tool output string"  # 关键：把局部变量 result 变成 str

    monkeypatch.setattr(td_mod, "load_skill_tools", lambda skill: [FakeTool()])

    log = str(tmp_path / "td2.log")
    tracer = DebugTracer(log)

    class FakeExec:
        def run_step(self, cmd, cwd=None, timeout=None):
            return {"returncode": 0, "stdout": "ok", "stderr": ""}

    class FakeAsm:
        def assemble(self, skill, arguments, plain_text=False):
            return "do something"

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            class R:
                content = "done"
                tool_calls = []
                additional_kwargs = {}
            r = R()
            if self.calls == 1:
                # 第一轮：让 LLM 调用 skill 工具，触发 result = tool_obj.invoke(...)
                r.tool_calls = [{"id": "t1", "type": "my_tool", "input": {"query": "x"}}]
            return r  # 第二轮无工具调用 → 正常完成

    meta = SkillMetadata(name="s", description="d")
    skill = Skill(metadata=meta, body="", directory=str(tmp_path))
    mr = MatchResult(skill=skill, score=1.0, method="name", arguments={})

    runner = ToolDispatchRunner(
        executor=FakeExec(), assembler=FakeAsm(), approval_fn=None,
        human_io=None, working_root=str(tmp_path), tracer=tracer,
    )
    res = runner.run(mr, FakeLLM(), max_iterations=3)  # 修复前这里会崩
    tracer.close()

    assert res.ctx["stopped_by"] in ("completed", "stop", "max_iterations")
    lines = [json.loads(l) for l in Path(log).read_text(encoding="utf-8").strip().splitlines()]
    kinds = [l["kind"] for l in lines]
    assert {"run_start", "iteration", "stop", "context"} <= set(kinds)

    stop_ev = [l for l in lines if l["kind"] == "stop"][0]
    assert stop_ev["stopped_by"] == res.ctx["stopped_by"]
    assert json.loads(Path(log + ".ctx.json").read_text(encoding="utf-8"))["stopped_by"] == res.ctx["stopped_by"]


# ---------- config.yml 来源（settings 经 backfill 落到环境变量） ----------

def test_config_yml_debug_log_backfill(monkeypatch):
    """config.yml settings.debug_log → 回填 SKILL_ENGINE_DEBUG_LOG（setdefault 语义）。"""
    from skill_engine.config import _apply_config_backfill
    import os
    monkeypatch.delenv("SKILL_ENGINE_DEBUG_LOG", raising=False)
    monkeypatch.delenv("SKILL_ENGINE_DEBUG", raising=False)
    _apply_config_backfill({"settings": {"debug_log": "/tmp/from_cfg.log"}})
    assert os.environ.get("SKILL_ENGINE_DEBUG_LOG") == "/tmp/from_cfg.log"


def test_config_yml_debug_switch_backfill(monkeypatch):
    """config.yml settings.debug: true → 回填 SKILL_ENGINE_DEBUG（布尔值可回填）。"""
    from skill_engine.config import _apply_config_backfill
    import os
    monkeypatch.delenv("SKILL_ENGINE_DEBUG_LOG", raising=False)
    monkeypatch.delenv("SKILL_ENGINE_DEBUG", raising=False)
    _apply_config_backfill({"settings": {"debug": True}})
    assert os.environ.get("SKILL_ENGINE_DEBUG") == "True"


def test_ci_env_overrides_config_yml(monkeypatch):
    """CI 注入的真实 env 优先于 config.yml 回填（setdefault 保证）。"""
    from skill_engine.config import _apply_config_backfill
    import os
    monkeypatch.setenv("SKILL_ENGINE_DEBUG_LOG", "/tmp/ci.log")
    _apply_config_backfill({"settings": {"debug_log": "/tmp/from_cfg.log"}})
    assert os.environ.get("SKILL_ENGINE_DEBUG_LOG") == "/tmp/ci.log"


def test_resolve_tracer_from_config_env(monkeypatch, tmp_path):
    """_resolve_debug_tracer 能从 config.yml 回填的 SKILL_ENGINE_DEBUG_LOG 开启。"""
    from skill_engine.cli import _resolve_debug_tracer
    log = str(tmp_path / "cfg.log")
    monkeypatch.setenv("SKILL_ENGINE_DEBUG_LOG", log)
    tr = _resolve_debug_tracer(debug=False, debug_log=None, working_root=str(tmp_path))
    assert tr.enabled() is True
    tr.close()


def test_resolve_tracer_config_debug_switch(monkeypatch, tmp_path):
    """config.yml settings.debug: true → 用默认路径开启。"""
    from skill_engine.cli import _resolve_debug_tracer
    monkeypatch.setenv("SKILL_ENGINE_DEBUG", "true")
    tr = _resolve_debug_tracer(debug=False, debug_log=None, working_root=str(tmp_path))
    assert tr.enabled() is True
    tr.close()
