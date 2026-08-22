"""回归测试：cli 的 session / run 命令必须把 trusted_root 透传到执行核心。

核心守住一个曾真实出现过的 bug：
  Session 模式（runner.run_repl）和普通 run 模式（runner.run_plan）在构造
  ToolDispatchRunner 时漏传 trusted_root，导致 `-w` 指定的工作空间内文件操作
  仍走 should_approve 审批，而 MOA 模式却能正确免审批——两种模式行为不一致。

修复后语义应与 MOA 完全一致：
  - 显式 -w   → trusted_root = working_root → 工作目录内文件操作自动放行
  - 未指定 -w → trusted_root = None          → 维持原审批（不启用目录信任）
"""

from unittest import mock

from typer.testing import CliRunner


def _fake_plan():
    """返回一个足以让 session / run 命令越过路由、直达 run_repl / run_plan 的假 plan。"""

    class _SkillStub:
        name = "x"

    class _Plan:
        method = "name"
        primary = _SkillStub()
        selections = []
        uncertain = False
        reason = ""
        score = 1.0

    return _Plan()


def test_session_passes_trusted_root_with_w(tmp_path):
    """显式 -w 时，session 命令必须把 working_root 作为 trusted_root 透传给 run_repl。"""
    captured = {}

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        def run_repl(self, *a, **k):
            captured.update(k)
            return {}

        def _check_approval(self, *a, **k):
            return True

    cli_runner = CliRunner()
    # 直接以 FakeRunner 替换 Runner 类
    with mock.patch("skill_engine.execution.runner.Runner", FakeRunner), \
         mock.patch("skill_engine.cli._normalize_working_root", lambda w: w), \
         mock.patch("skill_engine.cli._resolve_debug_tracer", lambda *a, **k: mock.MagicMock()), \
         mock.patch("skill_engine.cli._create_router") as m_router, \
         mock.patch("skill_engine.cli._get_tool_llm_client", lambda *a, **k: object()), \
         mock.patch("skill_engine.cli.print"):
        m_router.return_value.match.return_value = _fake_plan()
        import skill_engine.cli as cli
        # 模拟用户在 code-tutor-agent 上启动 session（-w 指定工作空间）
        cli_runner.invoke(cli.app, ["session", "-s", "x", "-w", str(tmp_path)])

    assert captured.get("trusted_root") == str(tmp_path)


def test_session_trusted_root_none_without_w():
    """未指定 -w 时，session 命令的 trusted_root 必须为 None（不启用目录信任）。"""
    captured = {}

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        def run_repl(self, *a, **k):
            captured.update(k)
            return {}

        def _check_approval(self, *a, **k):
            return True

    cli_runner = CliRunner()
    with mock.patch("skill_engine.execution.runner.Runner", FakeRunner), \
         mock.patch("skill_engine.cli._normalize_working_root", lambda w: w), \
         mock.patch("skill_engine.cli._resolve_debug_tracer", lambda *a, **k: mock.MagicMock()), \
         mock.patch("skill_engine.cli._create_router") as m_router, \
         mock.patch("skill_engine.cli._get_tool_llm_client", lambda *a, **k: object()), \
         mock.patch("skill_engine.cli.print"):
        m_router.return_value.match.return_value = _fake_plan()
        import skill_engine.cli as cli
        cli_runner.invoke(cli.app, ["session", "-s", "x"])  # 不带 -w

    assert captured.get("trusted_root") is None


def test_run_passes_trusted_root_with_w(tmp_path):
    """显式 -w 时，run 命令必须把 working_root 作为 trusted_root 透传给 run_plan。"""
    captured = {}

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        def run_plan(self, *a, **k):
            captured.update(k)
            return {}

        def _check_approval(self, *a, **k):
            return True

    cli_runner = CliRunner()
    with mock.patch("skill_engine.execution.runner.Runner", FakeRunner), \
         mock.patch("skill_engine.cli._normalize_working_root", lambda w: w), \
         mock.patch("skill_engine.cli._resolve_debug_tracer", lambda *a, **k: mock.MagicMock()), \
         mock.patch("skill_engine.cli._create_router") as m_router, \
         mock.patch("skill_engine.cli._get_tool_llm_client", lambda *a, **k: object()), \
         mock.patch("skill_engine.cli.print"):
        m_router.return_value.match.return_value = _fake_plan()
        import skill_engine.cli as cli
        cli_runner.invoke(cli.app, ["run", "x", "--tool-dispatch", "-w", str(tmp_path)])

    assert captured.get("trusted_root") == str(tmp_path)


def test_run_trusted_root_none_without_w():
    """未指定 -w 时，run 命令的 trusted_root 必须为 None（不启用目录信任）。"""
    captured = {}

    class FakeRunner:
        def __init__(self, *a, **k):
            pass

        def run_plan(self, *a, **k):
            captured.update(k)
            return {}

        def _check_approval(self, *a, **k):
            return True

    cli_runner = CliRunner()
    with mock.patch("skill_engine.execution.runner.Runner", FakeRunner), \
         mock.patch("skill_engine.cli._normalize_working_root", lambda w: w), \
         mock.patch("skill_engine.cli._resolve_debug_tracer", lambda *a, **k: mock.MagicMock()), \
         mock.patch("skill_engine.cli._create_router") as m_router, \
         mock.patch("skill_engine.cli._get_tool_llm_client", lambda *a, **k: object()), \
         mock.patch("skill_engine.cli.print"):
        m_router.return_value.match.return_value = _fake_plan()
        import skill_engine.cli as cli
        cli_runner.invoke(cli.app, ["run", "x", "--tool-dispatch"])  # 不带 -w

    assert captured.get("trusted_root") is None
