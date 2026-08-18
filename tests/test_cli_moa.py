"""回归测试：cli 的 MOA 执行入口。

核心守住一个曾真实出现过的 bug：
  `cli._moa_execute` 在未指定 `-w`（working_root=None）时，必须能回退到
  `str(Path.cwd())` 而不抛 `NameError: name 'Path' is not defined`。
根因：`Path` 只在 `moa()` 命令函数内局部 `from pathlib import Path`，
而 `_moa_execute` 是另一个函数，作用域内原本没有 `Path`。
"""

from unittest import mock

import json
import pathlib
from pathlib import Path

import pytest

import skill_engine.cli as cli


def test_moa_execute_falls_back_to_cwd_when_no_working_root():
    """working_root=None 时回退 cwd，且不抛 NameError。"""
    import pathlib

    captured = {}

    class FakeOrch:
        def __init__(self, **kw):
            captured.update(kw)

        def run(self, *a, **k):
            captured["run_called"] = True
            return {
                "rounds": 0, "llm_calls": 0, "tokens_total": 0,
                "tokens_prompt": 0, "tokens_completion": 0,
                "stopped_by": "test", "files_created": [],
            }

    class FakeRunner:
        def __init__(self, *a, **k):
            captured["runner_kw"] = k

        def _check_approval(self, *a, **k):
            return True

    with mock.patch("skill_engine.execution.executor.Executor", lambda **k: object()), \
         mock.patch("skill_engine.execution.assembler.Assembler", lambda **k: object()), \
         mock.patch("skill_engine.execution.runner.Runner", FakeRunner), \
         mock.patch("skill_engine.execution.moa.MoaOrchestrator", FakeOrch):
        # 不传 working_root（模拟未指定 -w）
        cli._moa_execute(
            query="t", registry=object(), workers=[], commander=object(),
            working_root=None, max_rounds=8, max_agent_iterations=12,
            max_llm_calls=60, verbose=False,
        )

    assert captured.get("run_called") is True
    assert captured.get("working_root") == str(pathlib.Path.cwd())


def test_moa_menu_keys_shortcut_selects_option():
    """确认菜单 label 里标注的 (y)/(x)/(r)/(e) 快捷键必须真实生效。"""
    class FakeHio:
        def __init__(self, answers):
            self._a = list(answers)

        def read(self, prompt=""):
            return self._a.pop(0)

    opts = [("start", "开始执行 (y)"), ("export", "仅导出配置 JSON (x)"),
            ("reconfig", "重新配置 (r)"), ("exit", "退出 (e)")]
    assert cli._moa_menu(FakeHio(["y"]), "t", opts, keys=["y", "x", "r", "e"]) == "start"
    assert cli._moa_menu(FakeHio(["X"]), "t", opts, keys=["y", "x", "r", "e"]) == "export"
    assert cli._moa_menu(FakeHio(["r"]), "t", opts, keys=["y", "x", "r", "e"]) == "reconfig"
    assert cli._moa_menu(FakeHio(["e"]), "t", opts, keys=["y", "x", "r", "e"]) == "exit"
    assert cli._moa_menu(FakeHio(["start"]), "t", opts, keys=["y", "x", "r", "e"]) == "start"
    assert cli._moa_menu(FakeHio(["4"]), "t", opts, keys=["y", "x", "r", "e"]) == "exit"


def test_moa_execute_passes_trusted_root(tmp_path):
    """用户显式指定 -w 时，trusted_root 必须透传到 MoaOrchestrator。"""
    captured = {}

    class FakeOrch:
        def __init__(self, **kw):
            captured.update(kw)

        def run(self, *a, **k):
            return {
                "rounds": 0, "llm_calls": 0, "tokens_total": 0,
                "tokens_prompt": 0, "tokens_completion": 0,
                "stopped_by": "test", "files_created": [],
            }

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        def _check_approval(self, *a, **k):
            return True

    with mock.patch("skill_engine.execution.executor.Executor", lambda **k: object()), \
         mock.patch("skill_engine.execution.assembler.Assembler", lambda **k: object()), \
         mock.patch("skill_engine.execution.runner.Runner", FakeRunner), \
         mock.patch("skill_engine.execution.moa.MoaOrchestrator", FakeOrch), \
         mock.patch("skill_engine.cli.print"):
        cli._moa_execute(
            query="t", registry=object(), workers=[], commander=object(),
            working_root=str(tmp_path), max_rounds=8, max_agent_iterations=12,
            max_llm_calls=60, verbose=False, trusted_root=str(tmp_path),
        )

    assert captured.get("trusted_root") == str(tmp_path)


def test_moa_execute_trusted_root_none_by_default():
    """未指定 -w 时 trusted_root 必须为 None（不启用目录信任）。"""
    captured = {}

    class FakeOrch:
        def __init__(self, **kw):
            captured.update(kw)

        def run(self, *a, **k):
            return {
                "rounds": 0, "llm_calls": 0, "tokens_total": 0,
                "tokens_prompt": 0, "tokens_completion": 0,
                "stopped_by": "test", "files_created": [],
            }

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        def _check_approval(self, *a, **k):
            return True

    with mock.patch("skill_engine.execution.executor.Executor", lambda **k: object()), \
         mock.patch("skill_engine.execution.assembler.Assembler", lambda **k: object()), \
         mock.patch("skill_engine.execution.runner.Runner", FakeRunner), \
         mock.patch("skill_engine.execution.moa.MoaOrchestrator", FakeOrch), \
         mock.patch("skill_engine.cli.print"):
        cli._moa_execute(
            query="t", registry=object(), workers=[], commander=object(),
            working_root=None, max_rounds=8, max_agent_iterations=12,
            max_llm_calls=60, verbose=False,
        )

    assert captured.get("trusted_root") is None


def test_moa_run_agent_collects_files_created(tmp_path):
    """回归：_run_agent 必须从 RunResult 取到 files_created（曾因 get("ctx") 恒空显示 0 个文件）。"""
    from skill_engine.execution.moa import MoaOrchestrator, MoaSession
    from skill_engine.models import MoaAgent, RunResult

    class FakeToolRunner:
        def __init__(self, **kw):
            pass

        def run(self, *a, **k):
            return RunResult(
                output="A1 实现登录函数",
                ctx={"steps": [], "files_created": [str(tmp_path / "index.html")],
                     "skill_name": "moa-builtin-A1", "iterations": 1,
                     "stopped_by": "stop"},
                history=[],
            )

    orch = MoaOrchestrator(executor=object(), assembler=object(), human_io=None,
                           working_root=str(tmp_path))
    session = MoaSession(working_root=str(tmp_path))
    agent = MoaAgent(alias="A1", model_profile="default", skill_name="",
                     instruction="开发", role="worker")

    with mock.patch("skill_engine.execution.moa.ToolDispatchRunner", FakeToolRunner):
        out, files, _ = orch._run_agent(agent, session, "实现登录", "做个登录页", 1, 10)

    assert files == [str(tmp_path / "index.html")]
    assert "登录" in out


def test_moa_pick_commander_skill_single_auto(monkeypatch):
    """白名单只有一个 → 不弹菜单直接使用（友好提示），不读用户输入。"""
    from skill_engine.execution import moa as moa_mod
    monkeypatch.setattr(moa_mod, "MOA_COMMANDER_SKILLS", ("moa-commander",))

    class FakeHio:
        def __init__(self):
            self.reads = 0

        def read(self, prompt=""):
            self.reads += 1
            return ""

    hio = FakeHio()
    picked = cli._moa_pick_commander_skill(hio, ["moa-commander", "code-builder"])
    assert picked == "moa-commander"
    assert hio.reads == 0


def test_moa_pick_commander_skill_multiple_asks(monkeypatch):
    """白名单扩展为多个 → 弹菜单选择。"""
    from skill_engine.execution import moa as moa_mod
    monkeypatch.setattr(moa_mod, "MOA_COMMANDER_SKILLS", ("moa-commander", "moa-auditor"))

    hio = mock.Mock()
    hio.read.return_value = "2"
    picked = cli._moa_pick_commander_skill(hio, ["moa-commander", "moa-auditor", "code-builder"])
    assert picked == "moa-auditor"


def test_moa_pick_commander_skill_none_falls_back(monkeypatch):
    """白名单均不可用 → 回退「内置 / 任意 skill」菜单。"""
    from skill_engine.execution import moa as moa_mod
    monkeypatch.setattr(moa_mod, "MOA_COMMANDER_SKILLS", ("moa-commander",))

    hio = mock.Mock()
    hio.read.return_value = "2"   # 菜单第 2 项 = code-builder
    picked = cli._moa_pick_commander_skill(hio, ["code-builder"])
    assert picked == "code-builder"


def test_moa_pick_commander_skill_off_skill_filtered(monkeypatch):
    """白名单命中但 state=off（不在 active_skills）→ 视为不可用。"""
    from skill_engine.execution import moa as moa_mod
    monkeypatch.setattr(moa_mod, "MOA_COMMANDER_SKILLS", ("moa-commander",))

    hio = mock.Mock()
    hio.read.return_value = "2"   # 回退菜单第 2 项 = code-builder
    picked = cli._moa_pick_commander_skill(hio, ["code-builder"])
    assert picked == "code-builder"


def test_moa_export_config_writes_plan_compatible_json(tmp_path):
    """导出的 JSON 必须能被 --plan 原样复用（agents/commander/query/options 四段）。"""
    from skill_engine.models import MoaAgent

    workers = [
        MoaAgent(alias="A1", model_profile="default", skill_name="code-builder",
                 instruction="实现登录", role="worker"),
        MoaAgent(alias="A2", model_profile="gpt4o", skill_name="",
                 instruction="审查", role="worker"),
    ]
    commander = MoaAgent(alias="C", model_profile="default", skill_name="",
                         instruction="达到质量门禁即 STOP", role="commander")
    out = tmp_path / "exports"
    out.mkdir()

    p = cli._moa_export_config(workers, commander, "做个登录页", 5, 10, 50,
                               str(out), source="wizard")

    cfg = json.loads(Path(p).read_text(encoding="utf-8"))
    assert Path(p).name.startswith("moa_config_") and Path(p).name.endswith(".json")
    assert [a["alias"] for a in cfg["agents"]] == ["A1", "A2"]
    assert cfg["agents"][1]["model_profile"] == "gpt4o"
    assert cfg["commander"]["alias"] == "C"
    assert cfg["query"] == "做个登录页"
    assert cfg["options"] == {"max_rounds": 5, "max_agent_iterations": 10, "max_llm_calls": 50}
    assert cfg["meta"]["source"] == "wizard"


def test_moa_execute_export_after_writes_config_and_prints_path(tmp_path):
    """export_after=True 时执行完毕自动导出配置 JSON 并打印路径。"""
    captured = {}

    class FakeOrch:
        def __init__(self, **kw):
            captured.update(kw)

        def run(self, *a, **k):
            captured["run_called"] = True
            return {
                "rounds": 1, "llm_calls": 2, "tokens_total": 0,
                "tokens_prompt": 0, "tokens_completion": 0,
                "stopped_by": "test", "files_created": [],
            }

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        def _check_approval(self, *a, **k):
            return True

    from skill_engine.models import MoaAgent
    workers = [MoaAgent(alias="A1", model_profile="default", skill_name="",
                        instruction="x", role="worker")]
    commander = MoaAgent(alias="C", model_profile="default", skill_name="",
                         instruction="y", role="commander")

    with mock.patch("skill_engine.execution.executor.Executor", lambda **k: object()), \
         mock.patch("skill_engine.execution.assembler.Assembler", lambda **k: object()), \
         mock.patch("skill_engine.execution.runner.Runner", FakeRunner), \
         mock.patch("skill_engine.execution.moa.MoaOrchestrator", FakeOrch), \
         mock.patch("skill_engine.cli.print") as fake_print:
        cli._moa_execute(
            query="t", registry=object(), workers=workers, commander=commander,
            working_root=str(tmp_path), max_rounds=8, max_agent_iterations=12,
            max_llm_calls=60, verbose=False,
            export_after=True, export_source="wizard",
        )

    exports = [f for f in tmp_path.iterdir() if f.name.startswith("moa_config_")]
    assert len(exports) == 1
    cfg = json.loads(exports[0].read_text(encoding="utf-8"))
    assert cfg["commander"]["alias"] == "C"
    assert cfg["options"]["max_rounds"] == 8
    assert cfg["meta"]["source"] == "wizard"
    assert any("已导出" in str(c) for c in fake_print.call_args_list)
