"""沙箱路由与 fail-closed 语义单测（不依赖 srt 后端，纯逻辑，毫秒级）。

覆盖设计文档 §4.7.9「白名单豁免 + 默认进沙箱」的路由矩阵，以及 §4.7 的
fail-closed 纪律：**沙箱该进却起不来时宁可不执行，也不能静默退化裸跑**。

真实 srt 后端的集成验证（ACL / 网络 / 机密文件）见
``tests/test_sandbox_integration.py`` —— 那个文件需要显式 opt-in。

已知缺口（测试里显式钉住现状，改代码时这几条会红，提醒你同步决策）：
- 脚本内删除（``python -c "shutil.rmtree(...)"`` / ``find . -delete``）不进 ASK；
- ``_CONTROL_OPS_RE`` 刻意不含单个 ``|``，只读管道直接裸跑。
"""

import sys
from pathlib import Path

import pytest

from skill_engine.execution.executor import Executor
from skill_engine.security.sandbox import (
    ROUTE_ASK,
    ROUTE_DIRECT,
    ROUTE_SANDBOX,
    decide_route,
)


# ---------------------------------------------------------------- 路由矩阵

@pytest.mark.parametrize("cmd", ["", "   ", "\n"])
def test_empty_command_is_direct(cmd):
    route, reason = decide_route(cmd)
    assert route == ROUTE_DIRECT
    assert "空命令" in reason


@pytest.mark.parametrize("cmd", [
    "rm -rf build",
    "rmdir foo",
    "del a.txt",
    "erase a.txt",
    "Remove-Item x -Force",
    "unlink /tmp/x",
    "/usr/bin/rm foo",            # 带路径 → 按 basename 判
    r"D:\tools\rm.exe foo",
    "ls && rm foo.txt",           # 组合命令里含删除动词 → 删除优先级最高
    "cat x | rm y",
    "find . -exec rm {} ;",       # -exec 的 rm 也能被词扫到
])
def test_delete_verbs_go_to_ask(cmd):
    """删除类命令不进沙箱：沙箱内 rm 绕过宿主 safe-delete（实测硬删）。"""
    route, reason = decide_route(cmd)
    assert route == ROUTE_ASK, cmd
    assert "删除类命令" in reason


@pytest.mark.parametrize("cmd", [
    "curl https://example.com",
    "wget http://x/y",
    "ssh user@host",
    "scp a b",
    "ping -n 1 1.1.1.1",
    "Invoke-WebRequest http://x",
])
def test_network_verbs_go_to_ask(cmd):
    """沙箱内网络被 WFP 全拦（含回环）→ 联网命令走审批后裸跑。"""
    route, reason = decide_route(cmd)
    assert route == ROUTE_ASK, cmd
    assert "联网命令" in reason


@pytest.mark.parametrize("cmd", [
    "uv sync",
    "uv run pytest",
    "pip install requests",
    "npm install",
    "pnpm build",
    "yarn test",
    "gh pr list",
    "docker ps",
    "winget install x",
])
def test_host_tool_verbs_go_to_ask(cmd):
    """宿主工具需要 AppData 缓存/凭据，沙箱用户不可读 → 走审批后裸跑。"""
    route, reason = decide_route(cmd)
    assert route == ROUTE_ASK, cmd
    assert "宿主工具" in reason


@pytest.mark.parametrize("cmd,expected", [
    ("git status", ROUTE_DIRECT),
    ("git diff HEAD", ROUTE_DIRECT),
    ("git log --oneline -5", ROUTE_DIRECT),
    ("git rev-parse HEAD", ROUTE_DIRECT),
    ("git ls-files", ROUTE_DIRECT),
    ("git -C D:/repo status", ROUTE_DIRECT),          # 取值选项不能被子命令解析吃掉
    ("git --git-dir=D:/r/.git log", ROUTE_DIRECT),
    ("git -c core.pager=cat diff", ROUTE_DIRECT),
    ("git --version", ROUTE_DIRECT),                  # 无子命令形态
    ("git push origin main", ROUTE_ASK),
    ("git commit -m 'x'", ROUTE_ASK),
    ("git clone https://x/y", ROUTE_ASK),
    ("git fetch", ROUTE_ASK),
    ("git add .", ROUTE_ASK),
    ("git log > out.txt", ROUTE_SANDBOX),             # 只读子命令 + 重定向 → 沙箱
])
def test_git_routing(cmd, expected):
    route, _reason = decide_route(cmd)
    assert route == expected, cmd


@pytest.mark.parametrize("cmd", [
    "ls -la",
    "cat README.md",
    "grep -rn foo src",
    "head -20 a.txt",
    "tail -f a.log",
    "wc -l a.txt",
    "find . -name '*.py'",
    "pwd",
    "echo hello",
    "rg pattern",
])
def test_readonly_verbs_are_direct(cmd):
    """纯只读白名单 → 裸跑，省掉 ~0.35s/次沙箱包装成本。"""
    route, _reason = decide_route(cmd)
    assert route == ROUTE_DIRECT, cmd


@pytest.mark.parametrize("cmd", [
    "ls > out.txt",
    "cat a.txt && cat b.txt",
    "echo x; ls",
    "echo $(whoami)",
    "cat `pwd`/x",
    "cat < input.txt",
])
def test_readonly_with_control_ops_downgrades_to_sandbox(cmd):
    """只读动词 + 重定向/串联/命令替换 → 只读语义不成立，降级进沙箱。"""
    route, reason = decide_route(cmd)
    assert route == ROUTE_SANDBOX, cmd
    assert "降级进沙箱" in reason


def test_single_pipe_is_not_a_control_op():
    """刻意决策：单个 `|` 不算控制符（两端都是只读动词时没必要付沙箱成本）。"""
    assert decide_route("echo x | grep y")[0] == ROUTE_DIRECT


@pytest.mark.parametrize("cmd", [
    "sed -i 's/a/b/' f.txt",
    "sed -i.bak 's/a/b/' f.txt",
    "sort -o out.txt in.txt",
    "awk -i inplace '{print}' f.txt",
])
def test_inplace_write_flags_downgrade_to_sandbox(cmd):
    """设计文档 §4.7.9：`sed -i` 这类就地写不算只读，必须进沙箱。"""
    route, reason = decide_route(cmd)
    assert route == ROUTE_SANDBOX, cmd
    assert "就地写开关" in reason


@pytest.mark.parametrize("cmd", [
    "grep -i needle f.txt",       # -i 是忽略大小写，不是就地写
    "rg -i needle",
    "sed -n '1,5p' f.txt",
    "sort -n nums.txt",
])
def test_inplace_flag_does_not_overmatch(cmd):
    """只按动词分派地匹配就地写开关，避免 `grep -i` 被误判。"""
    assert decide_route(cmd)[0] == ROUTE_DIRECT, cmd


@pytest.mark.parametrize("cmd", [
    "python script.py",
    "mkdir foo",
    "touch a.txt",
    "pytest -q",
    "make build",
    "./run.sh",
    "some_unknown_thing --flag",
    "cp a b",
    "mv a b",
])
def test_default_is_sandbox(cmd):
    """未知 / 有副作用的命令默认进沙箱（白名单豁免 + 默认进沙箱）。"""
    route, reason = decide_route(cmd)
    assert route == ROUTE_SANDBOX, cmd
    assert "默认进沙箱" in reason


@pytest.mark.parametrize("cmd,expected", [
    # 这两条比 P1 之前更差：原先被宿主守卫拦下，现在进沙箱 → 沙箱内硬删
    ('python -c "import shutil; shutil.rmtree(\'build\')"', ROUTE_SANDBOX),
    ("python cleanup.py", ROUTE_SANDBOX),
    ("bash cleanup.sh", ROUTE_SANDBOX),
    # 这条不算变差：裸跑时宿主守卫仍生效，只是没有审批、且白名单名不副实
    ("find . -delete", ROUTE_DIRECT),
])
def test_script_level_delete_is_known_gap(cmd, expected):
    """**已知缺口**：脚本内的删除等价物识别不了（本清单只认命令词）。

    精确修法见 ``security/sandbox.py`` 中 ``_DELETE_VERBS`` 上方的补丁说明。
    若哪天采纳，把上表期望统一改成 ROUTE_ASK —— 这几条红就是提醒。
    """
    assert decide_route(cmd)[0] == expected


# ---------------------------------------------------------------- fail-closed 语义

class TestExecutorSandboxFailClosed:
    """Executor 侧的沙箱接线：真失败必须拒绝执行，绝不静默裸跑。"""

    def _exec(self, **kw):
        ex = Executor(timeout=10, **kw)
        ex._sandbox_forced_off = False   # 测试环境默认压掉沙箱，这里显式放开
        return ex

    def test_wrap_failure_refuses_execution(self, tmp_path, monkeypatch):
        """srt 可用但本次包装失败 → 返回拒绝执行，且命令没有真的跑。"""
        ex = self._exec()
        monkeypatch.setattr(
            ex, "_wrap_sandbox",
            lambda c, cwd, env: (None, "[沙箱不可用，已拒绝执行] boom"),
        )
        res = ex.run_step("echo should-not-run", cwd=tmp_path, sandbox=True)
        assert res["exit_code"] == 1
        assert "拒绝执行" in res["stderr"]
        assert res["stdout"] == ""          # 没有退化裸跑
        assert res["sandbox"] is False

    def test_wrapped_success_reports_sandbox_true(self, tmp_path, monkeypatch):
        """包装成功时结果集必须标 sandbox=True（observation 依赖这个真实值）。"""
        ex = self._exec()
        monkeypatch.setattr(
            ex, "_wrap_sandbox",
            lambda c, cwd, env: ([sys.executable, "-c", "print('in-sandbox')"], ""),
        )
        res = ex.run_step("whatever", cwd=tmp_path, sandbox=True)
        assert res["exit_code"] == 0
        assert "in-sandbox" in res["stdout"]
        assert res["sandbox"] is True

    def test_test_env_upper_bound_suppresses_explicit_true(self, tmp_path, monkeypatch):
        """测试/CI 环境是「上限」：调用方显式传 sandbox=True 也压掉，不付会话成本。

        该标志由 ``get_sandbox_config()`` 依据 ``PYTEST_CURRENT_TEST`` / ``CI`` 置位
        ——但**只在没有设 ``SKILLS_ENGINE_SANDBOX`` 时**（显式 opt-in 会绕开它，
        集成测试正依赖这一点）。所以这里显式置位，而不是断言运行环境的默认值，
        否则本用例在「带 opt-in 跑全量」时会假红。
        """
        ex = Executor(timeout=10)
        ex._sandbox_forced_off = True
        called = []
        monkeypatch.setattr(
            ex, "_wrap_sandbox",
            lambda c, cwd, env: (called.append(1) or (None, "should not be called")),
        )
        res = ex.run_step("echo bare", cwd=tmp_path, sandbox=True)
        assert called == []
        assert res["exit_code"] == 0
        assert "bare" in res["stdout"]
        assert res["sandbox"] is False

    def test_wsl_shell_is_unsupported_and_blocks(self, tmp_path):
        """WSL 形态没有 srt 启动路径 → on_unavailable=block 时拒绝执行。"""
        ex = self._exec(shell="wsl")
        ex.sandbox_on_unavailable = "block"
        res = ex.run_step("ls", cwd=tmp_path, sandbox=True)
        assert res["exit_code"] == 1
        assert "不支持沙箱" in res["stderr"]

    def test_wsl_shell_warn_contract_is_bare_run(self, tmp_path):
        """on_unavailable=warn → 契约是 (None, "")：告警由调用方发，按裸跑继续。"""
        ex = self._exec(shell="wsl")
        ex.sandbox_on_unavailable = "warn"
        argv, err = ex._wrap_sandbox("ls", tmp_path, {})
        assert argv is None
        assert err == ""

    def test_result_always_carries_sandbox_key(self, tmp_path):
        """结果的早期返回路径也必须带 sandbox 键，避免调用方把「缺键」当「已沙箱」。"""
        ex = self._exec()
        res = ex.run_step("echo hi", cwd=Path("D:/definitely/not/a/dir"), sandbox=True)
        assert res["sandbox"] is False
        assert res["exit_code"] == 1

    def test_run_argv_is_bare_and_never_sandboxed(self, tmp_path):
        """run_argv 刻意不提供沙箱形态（rg 装在 AppData，沙箱用户读不到）。"""
        ex = self._exec(sandbox=True)     # 即便默认策略要求沙箱
        res = ex.run_argv([sys.executable, "-c", "print('argv-ok')"], cwd=tmp_path)
        assert res["exit_code"] == 0
        assert "argv-ok" in res["stdout"]
        assert res["sandbox"] is False

    def test_run_argv_rejects_empty_and_bad_cwd(self, tmp_path):
        ex = self._exec()
        assert ex.run_argv([], cwd=tmp_path)["exit_code"] == 1
        bad = ex.run_argv([sys.executable, "-c", "pass"], cwd=Path("D:/nope/nope"))
        assert bad["exit_code"] == 1
        assert "工作目录无效" in bad["stderr"]


# ---------------------------------------------------------------- search_files 收口

class _RecordingExecutor:
    """记录 run_argv 的 argv/cwd，用于验证 spawn 收口与 argv 形态。"""

    def __init__(self, stdout="a.py:1: needle  ← MATCH\n", exit_code=0):
        self.calls = []
        self._stdout = stdout
        self._exit = exit_code

    def run_argv(self, argv, cwd, timeout=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        return {"stdout": self._stdout, "stderr": "", "exit_code": self._exit,
                "timed_out": False, "sandbox": False}


# ---------------------------------------------------------------- 后端不可用 / 报告

class TestManagerUnavailable:
    """不碰真实 srt 的 Manager 行为（二进制发现失败、报告文案）。"""

    def test_missing_binary_marks_unavailable(self, tmp_path, monkeypatch):
        import skill_engine.security.sandbox as sb
        monkeypatch.delenv(sb.SRT_BIN_ENV, raising=False)
        monkeypatch.setattr(sb, "_NPX_CACHE_GLOBS", ())     # 断开 npx 缓存兜底
        mgr = sb.SandboxManager(tmp_path)
        assert mgr.ensure_ready() is False
        assert mgr._state == "unavailable"
        assert "未找到 srt-win.exe" in mgr.last_error

    def test_report_states_when_not_used(self, tmp_path):
        from skill_engine.security.sandbox import SandboxManager
        mgr = SandboxManager(tmp_path)
        assert "尚未启用" in mgr.capability_report()

    def test_report_states_when_unavailable(self, tmp_path, monkeypatch):
        import skill_engine.security.sandbox as sb
        monkeypatch.delenv(sb.SRT_BIN_ENV, raising=False)
        monkeypatch.setattr(sb, "_NPX_CACHE_GLOBS", ())
        mgr = sb.SandboxManager(tmp_path)
        mgr.ensure_ready()
        report = mgr.capability_report()
        assert "沙箱不可用" in report

    def test_capability_matrix_reports_drive_d_gap(self):
        """能力矩阵必须如实报出「D 盘写挡不住」，不做粉饰。"""
        from skill_engine.security.sandbox import CAPABILITY_MATRIX
        rows = {name: (verdict, note) for name, verdict, note in CAPABILITY_MATRIX}
        assert any("D 盘" in name for name in rows)
        d_verdict, d_note = next(v for k, v in rows.items() if "D 盘" in k)
        assert "挡不住" in d_verdict
        assert "DENY" in d_note
        assert any("删除" in name for name in rows)

    def test_close_is_idempotent_without_setup(self, tmp_path):
        from skill_engine.security.sandbox import SandboxManager
        mgr = SandboxManager(tmp_path)
        mgr.close()
        mgr.close()          # 未 ready 时直接 return，不应抛


class TestRipgrepSpawnColocation:
    def test_rg_spawn_goes_through_executor_argv(self, tmp_path, monkeypatch):
        """rg 的 spawn 收口到 Executor.run_argv：pattern 作为独立 argv 元素，
        不经 shell 解析，因此带引号/空格/& 也不会被二次转义。"""
        import skill_engine.execution.tool_exec.search as ts
        monkeypatch.setattr(ts.shutil, "which", lambda name: r"C:\fake\rg.exe")
        pattern = 'foo "bar" & baz'
        fake = _RecordingExecutor()
        out = ts._run_ripgrep(pattern, tmp_path, "*.py", 100, 3, executor=fake)

        assert len(fake.calls) == 1
        argv = fake.calls[0]["argv"]
        assert argv[0] == r"C:\fake\rg.exe"
        assert "--" in argv
        sep = argv.index("--")
        assert argv[sep + 1] == pattern          # pattern 原样独立成元素
        assert argv[sep + 2] == "."
        assert fake.calls[0]["timeout"] == ts._RG_TIMEOUT
        assert "needle" in out

    def test_falls_back_to_python_when_executor_reports_failure(self, tmp_path, monkeypatch):
        """rg 非 0/1 退出（超时/权限）时返回 None → 调用方回退纯 Python。"""
        import skill_engine.execution.tool_exec.search as ts
        monkeypatch.setattr(ts.shutil, "which", lambda name: r"C:\fake\rg.exe")
        fake = _RecordingExecutor(exit_code=-1)
        assert ts._run_ripgrep("x", tmp_path, "", 100, 3, executor=fake) is None

    def test_search_files_entry_passes_executor_down(self, tmp_path, monkeypatch):
        import skill_engine.execution.tool_exec.search as ts
        seen = {}

        def fake_rg(pattern, search_dir, file_glob, max_results, context_lines, executor=None):
            seen["executor"] = executor
            return "hit"

        monkeypatch.setattr(ts, "_run_ripgrep", fake_rg)
        marker = object()
        ts._search_files("x", tmp_path, executor=marker)
        assert seen["executor"] is marker
