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
        """构造一个「沙箱策略已放开」的 Executor。

        pytest 环境下 ``get_sandbox_config()`` 会把 ``enabled`` 置 False 并把
        ``forced_off_by_test_env`` 置 True（省掉 10s+ 会话准备成本），这里是
        反向操作——**两层都要放开**：只清 ``_sandbox_forced_off`` 而不管
        ``sandbox_enabled``，在「策略层压请求层」的语义下沙箱依然进不去，
        用例会从「测包装失败」退化成「测裸跑」（2026-09-18 语义重排时踩到）。
        """
        ex = Executor(timeout=10, **kw)
        ex.sandbox_enabled = True
        ex._sandbox_forced_off = False
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
        集成测试正依赖这一点）。所以这里显式置位、并同时打开 ``sandbox_enabled``，
        排除「靠 pytest 环境默认值蒙对」的可能，否则本用例在「带 opt-in 跑全量」
        时会假红。
        """
        ex = Executor(timeout=10)
        ex.sandbox_enabled = True        # 策略层本身是开的……
        ex._sandbox_forced_off = True    # ……但测试上限把它压掉
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


# ---------------------------------------------------- 沙箱开关：策略层 vs 请求层

class TestSandboxToggleSemantics:
    """``settings.sandbox.enabled: false`` / ``SKILLS_ENGINE_SANDBOX=off`` 必须真能关掉沙箱。

    回归自 2026-09-18 的缺陷：旧实现写作
    ``use_sandbox = sandbox_enabled if sandbox is None else bool(sandbox)``
    —— 传参一旦非 None 就架空策略层。而 ``bash`` / ``run_python`` 两个 handler
    **永远**传显式布尔（``use_sandbox = route == ROUTE_SANDBOX``，见
    ``handlers/bash.py``），于是 ``enabled: false`` 对这两条主干活路径完全失灵，
    配置项形同虚设（复现脚本 ``.skill-engine/toggle_probe.py``）。

    正确语义：**策略层是上限，请求层只能在上限之内要求「本条不要沙箱」**，
    不能反过来要求沙箱。层级顺序：策略关闭 > 请求豁免 > 配置默认。
    """

    def _exec(self, **kw):
        """清掉 pytest 的强制上限，**但不动 sandbox_enabled**。

        这里要测的正是「构造值（＝配置）能不能压住传参」，所以策略层开关必须
        由 ``sandbox=`` 构造参数决定，不能被 helper 顺手改成 True。
        （构造参数优先级最高，见 ``Executor.__init__``：``enabled`` 只在
        参数为 None 时才从配置读——所以 pytest 环境下 ``sandbox=True/False``
        依然如实生效。）
        """
        ex = Executor(timeout=10, **kw)
        ex._sandbox_forced_off = False
        return ex

    def _spy_wrap(self, monkeypatch, ex):
        """记录 ``_wrap_sandbox`` 是否被调用——没被调用就等于证明「走了裸跑」。"""
        calls = []

        def fake(command, cwd, env):
            calls.append(command)
            return [sys.executable, "-c", "print('in-sandbox')"], ""

        monkeypatch.setattr(ex, "_wrap_sandbox", fake)
        return calls

    def test_config_disabled_overrides_explicit_true(self, tmp_path, monkeypatch):
        """★ 缺陷现场：配置关闭 + handler 传显式 True → 必须裸跑。"""
        ex = self._exec(sandbox=False)
        assert ex.sandbox_enabled is False
        calls = self._spy_wrap(monkeypatch, ex)
        res = ex.run_step("echo bare", cwd=tmp_path, sandbox=True)
        assert calls == [], "配置关闭了，包装层不该被碰"
        assert res["exit_code"] == 0
        assert "bare" in res["stdout"]
        assert res["sandbox"] is False

    @pytest.mark.parametrize("requested", [None, True, False])
    def test_config_disabled_bare_for_every_request(
        self, tmp_path, monkeypatch, requested
    ):
        """参数矩阵补全：配置关闭时 None/True/False 三种传参一律裸跑。"""
        ex = self._exec(sandbox=False)
        calls = self._spy_wrap(monkeypatch, ex)
        res = ex.run_step("echo bare", cwd=tmp_path, sandbox=requested)
        assert calls == [], f"requested={requested} 竟然进了沙箱"
        assert res["sandbox"] is False

    def test_request_false_wins_when_config_enabled(self, tmp_path, monkeypatch):
        """配置开启 + 路由豁免传 False → 裸跑（请求层**向下**依然有效）。"""
        ex = self._exec(sandbox=True)
        calls = self._spy_wrap(monkeypatch, ex)
        res = ex.run_step("echo bare", cwd=tmp_path, sandbox=False)
        assert calls == []
        assert res["sandbox"] is False

    def test_request_none_falls_back_to_config(self, tmp_path, monkeypatch):
        """不表态（None）时沿用配置：开则进沙箱，且必须在沙箱形态下执行。"""
        ex = self._exec(sandbox=True)
        calls = self._spy_wrap(monkeypatch, ex)
        res = ex.run_step("echo hi", cwd=tmp_path, sandbox=None)
        assert calls == ["echo hi"]
        assert res["sandbox"] is True

    def test_fail_closed_not_weakened_by_toggle_semantics(
        self, tmp_path, monkeypatch
    ):
        """语义重排后 fail-closed 不变：配置开启 + 包装失败 → 仍拒绝执行。"""
        ex = self._exec(sandbox=True)
        monkeypatch.setattr(
            ex, "_wrap_sandbox", lambda c, cwd, env: (None, "[沙箱不可用，已拒绝执行] boom")
        )
        res = ex.run_step("echo nope", cwd=tmp_path, sandbox=True)
        assert res["exit_code"] == 1
        assert res["stdout"] == ""
        assert res["sandbox"] is False

    def test_handlers_still_pass_explicit_bool(self):
        """钉住修复的**前提**：bash/run_python 传显式布尔，而从不传 None。

        只要这条成立，「关不关得掉」就取决于 Executor 的策略层压不压得住——
        缺陷的正确修复位置在 Executor，不在 handler（handler 传得没错）。
        哪天有人把 handler 改成传 None（＝把决定权交回配置层），这条会红，
        提醒重新审视上层语义。
        """
        root = Path(__file__).resolve().parents[1]
        bash_src = (
            root / "src/skill_engine/execution/tool_exec/handlers/bash.py"
        ).read_text(encoding="utf-8")
        # bash：由路由判定派生（route == ROUTE_SANDBOX），仍是显式布尔
        assert "use_sandbox = route == ROUTE_SANDBOX" in bash_src
        assert "sandbox=use_sandbox" in bash_src

        py_src = (
            root / "src/skill_engine/execution/tool_exec/handlers/run_python.py"
        ).read_text(encoding="utf-8")
        # run_python：直接给字面量布尔（已审批 → False；否则 True）
        assert "sandbox=False)" in py_src and "sandbox=True)" in py_src
        assert "sandbox=sandbox" in py_src

        for rel, src in (("bash.py", bash_src), ("run_python.py", py_src)):
            assert "sandbox=None" not in src, (
                f"{rel} 开始传 sandbox=None —— 决定权已交回配置层，"
                "上方「策略层压请求层」的语义与这批用例需要一并重新审视"
            )


# ---------------------------------------------------- 沙箱配置解析（config.yml）

class TestSandboxConfigParsing:
    """``settings.sandbox.enabled`` 的类型归一化。

    回归自 2026-09-18（与上面 Executor 的开关缺陷是**两个独立**缺陷）：
    ``get_sandbox_config()`` 把该段的非列表值统一走
    ``os.path.expandvars(str(val))``，而 ``str(False) == "False"`` 是个非空
    字符串 → ``bool("False")`` 为 True → ``enabled: false`` 静默失效。
    ``Executor`` 侧修好之后，**配置文件这条路依然关不掉沙箱**，直到本处修复。
    """

    @staticmethod
    def _cfgmod_with(monkeypatch, section, env_toggle=None):
        """把 ``_load_config_yml`` 换成「真实配置 + 覆盖 settings.sandbox」。

        必须先摘掉 ``PYTEST_CURRENT_TEST`` / ``CI``：``get_sandbox_config()``
        在检测到测试环境且 ``SKILLS_ENGINE_SANDBOX`` 未设时，会**无条件**把
        ``enabled`` 压成 False（省掉每会话 ~10s 的 ACL 成本）。不摘掉的话，
        「配置文件写 false」的用例会靠这个环境默认值蒙对，而「写 true」的用例
        则永远断言不过——正是这个上限让**配置文件路径无法在 pytest 内验证**，
        所以另有独立探针 ``.skill-engine/config_toggle_probe.py`` 在 pytest 外复测。
        """
        import skill_engine.config as cfgmod

        real = cfgmod._load_config_yml()

        def fake():
            cfg = dict(real)
            settings = dict(cfg.get("settings") or {})
            settings["sandbox"] = section
            cfg["settings"] = settings
            return cfg

        monkeypatch.setattr(cfgmod, "_load_config_yml", fake)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("CI", raising=False)
        # 环境变量优先级高于配置文件，会把 enabled 覆盖掉——本组用例只测
        # 「配置文件怎么写就怎么解析」，所以先摘掉它（带 opt-in 跑全量时不摘会假红）
        if env_toggle is None:
            monkeypatch.delenv("SKILLS_ENGINE_SANDBOX", raising=False)
        else:
            monkeypatch.setenv("SKILLS_ENGINE_SANDBOX", env_toggle)
        return cfgmod

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (True, True), (False, False),
            ("true", True), ("false", False),
            ("True", True), ("False", False),
            ("yes", True), ("no", False),
            ("on", True), ("off", False),
            ("1", True), ("0", False),
            (1, True), (0, False),
        ],
    )
    def test_enabled_normalised_to_real_bool(self, monkeypatch, raw, expected):
        cfgmod = self._cfgmod_with(monkeypatch, {"enabled": raw})
        got = cfgmod.get_sandbox_config()["enabled"]
        assert got is expected, f"enabled={raw!r} 解析成了 {got!r}，不是真 bool"

    def test_unknown_value_falls_back_to_safe_default(self, monkeypatch):
        """无法识别的写法回落到默认值（True = 保持沙箱开启，失败方向偏安全）。"""
        cfgmod = self._cfgmod_with(monkeypatch, {"enabled": "perhaps"})
        assert cfgmod.get_sandbox_config()["enabled"] is True

    def test_missing_section_and_bare_string(self, monkeypatch):
        """不写 sandbox 段 → 默认；写成裸字符串 "" → 沿用默认（空值跳过）。"""
        cfgmod = self._cfgmod_with(monkeypatch, {})
        assert cfgmod.get_sandbox_config()["enabled"] is True
        cfgmod = self._cfgmod_with(monkeypatch, {"enabled": ""})
        assert cfgmod.get_sandbox_config()["enabled"] is True

    def test_env_toggle_still_overrides_file(self, monkeypatch):
        """环境变量是整体开关，压过配置文件（两条路径的汇合点）。"""
        cfgmod = self._cfgmod_with(
            monkeypatch, {"enabled": True}, env_toggle="off"
        )
        assert cfgmod.get_sandbox_config()["enabled"] is False

    def test_default_lists_are_not_shared_with_module_constant(self, monkeypatch):
        """默认值里的列表必须逐次复制，否则调用方改动会污染模块常量。"""
        cfgmod = self._cfgmod_with(monkeypatch, {})
        snapshot = list(cfgmod._SANDBOX_DEFAULTS["deny_read"])
        first = cfgmod.get_sandbox_config()
        first["deny_read"].append("C:/polluted-by-caller")
        assert cfgmod._SANDBOX_DEFAULTS["deny_read"] == snapshot
        assert "C:/polluted-by-caller" not in cfgmod.get_sandbox_config()["deny_read"]

    def test_disabled_file_actually_yields_bare_run(self, monkeypatch, tmp_path):
        """端到端合一：配置文件写 false → Executor 走裸跑（两条缺陷的联合回归）。"""
        cfgmod = self._cfgmod_with(monkeypatch, {"enabled": False})
        assert cfgmod.get_sandbox_config()["enabled"] is False
        ex = Executor(timeout=10)
        ex._sandbox_forced_off = False
        calls = []

        def fake_wrap(command, cwd, env):
            calls.append(command)
            return [sys.executable, "-c", "print('in-sandbox')"], ""

        monkeypatch.setattr(ex, "_wrap_sandbox", fake_wrap)
        res = ex.run_step("echo bare", cwd=tmp_path, sandbox=True)
        assert calls == [], "配置文件说关，bash 还是进了沙箱"
        assert res["sandbox"] is False
        assert "bare" in res["stdout"]


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
