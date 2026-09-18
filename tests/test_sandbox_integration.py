"""srt 沙箱集成测试 —— 真实调用 ``srt-win``，**默认整个文件跳过**。

为什么默认跳过：会话准备有一次性成本（本次实测 stamp 单个机密文件 ~0.5s +
能力探测 ~0.35s），并且会在工作目录内的 ``config.yml`` 上打 DENY ACE（会话结束
由 ``acl restore`` 回收）。让它默认参与常规单测，既拖慢 100+ 用例的回归，
也会让「沙箱坏了」和「业务代码坏了」混在一起。

启用（三个条件同时成立才真正执行）::

    SKILLS_ENGINE_SANDBOX=on python -m pytest tests/test_sandbox_integration.py -v

1. ``SKILLS_ENGINE_SANDBOX=on`` —— 显式 opt-in；顺带绕开
   ``config.get_sandbox_config()`` 的「测试环境强制关闭」上限；
2. ``os.name == "nt"`` —— srt-win 是 Windows 专用实现；
3. 能定位到 ``srt-win.exe``（环境变量 ``SKILL_ENGINE_SRT_WIN`` 或 npx 缓存）。

本文件的断言全部是**实测结论的回归护栏**（设计文档 §3.5 的能力矩阵来源）：
机密文件读被拒、工作区可写、外联与**回环**均被 WFP 拦、失败时 fail-closed。
"""

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from skill_engine.security.sandbox import SandboxManager

# ---------------------------------------------------------------- opt-in 闸门

_OPT_IN = os.environ.get("SKILLS_ENGINE_SANDBOX", "").strip().lower() in (
    "on", "1", "true", "yes", "enable",
)


def _srt_available() -> bool:
    """只做二进制发现（glob，不执行 srt），用于收集期的 skip 判定。"""
    if os.name != "nt":
        return False
    return bool(SandboxManager(Path.cwd())._resolve_srt_bin())


_HAVE_SRT = _srt_available() if _OPT_IN else False

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(not _OPT_IN,
                       reason="需显式 opt-in：SKILLS_ENGINE_SANDBOX=on"),
    pytest.mark.skipif(_OPT_IN and not _HAVE_SRT,
                       reason="未找到 srt-win.exe（npx 缓存 / SKILL_ENGINE_SRT_WIN）"),
]

_SECRET_TEXT = "s3cr3t-api-key-do-not-leak"


# ---------------------------------------------------------------- 夹具

@pytest.fixture(scope="module")
def workspace() -> Path:
    """沙箱工作目录。

    刻意**不用** ``tmp_path``：``tempfile.mkdtemp`` 建的目录只授权 SYSTEM /
    Administrators / OWNER RIGHTS，没有 ``Authenticated Users``，沙箱用户无法
    穿越，srt-win 会以误导性的 ``mapped_drive_cwd``（exit 16）报错 —— 见
    ``SandboxManager._explain_probe_failure``。放在仓库内子目录可继承 ``D:\\``
    的 ``Authenticated Users:(OI)(CI)(IO)(M)``，与真实工作区一致。

    ``.skill-engine/`` 已在 .gitignore 中，不会污染 git status。
    """
    repo_root = Path(__file__).resolve().parents[1]
    ws = repo_root / ".skill-engine" / "sandbox-it"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "config.yml").write_text(f"api_key: {_SECRET_TEXT}\n", encoding="utf-8")
    (ws / "README.md").write_text("readable content\n", encoding="utf-8")
    try:
        yield ws
    finally:
        # 只清文件、不删目录：目录本身无害且留在 gitignore 内，删目录会触发
        # 宿主 safe-delete 的批量确认/FAIL_CLOSED，得不偿失。
        for name in ("config.yml", "README.md", "out.txt"):
            try:
                (ws / name).unlink(missing_ok=True)
            except OSError:
                pass


@pytest.fixture(scope="module")
def mgr(workspace) -> SandboxManager:
    """会话级 SandboxManager（走 get() 复用，与 Executor 拿到同一实例）。"""
    manager = SandboxManager.get(workspace)
    assert manager.ensure_ready(), f"沙箱准备失败：{manager.last_error}"
    try:
        yield manager
    finally:
        manager.close()
        SandboxManager.forget(workspace)


def _exec(mgr: SandboxManager, command: str, ws: Path, shell: str = "cmd",
          timeout: int = 90) -> subprocess.CompletedProcess:
    """把 cmd 命令丢进沙箱执行（等价于 bash 工具激活沙箱时的那条通路）。"""
    argv = mgr.wrap(command, cwd=ws, env=dict(os.environ), shell=shell)
    assert argv is not None, f"wrap 返回 None：{mgr.last_error}"
    assert argv[1] == "exec", argv[:3]
    return subprocess.run(argv, capture_output=True, cwd=str(ws),
                          timeout=timeout, env=dict(os.environ))


def _out(p: subprocess.CompletedProcess) -> str:
    return (p.stdout or b"").decode("utf-8", errors="replace")


def _err(p: subprocess.CompletedProcess) -> str:
    return (p.stderr or b"").decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 会话准备

class TestSessionReadiness:
    def test_ready_and_reports_setup_cost(self, mgr):
        assert mgr._state == "ready"
        assert mgr.setup_ms > 0
        assert mgr._sid, "应能取到沙箱用户 SID"

    def test_secret_file_is_stamped(self, mgr, workspace):
        """工作目录里的 config.yml 应进 stamp 列表（否则机密文件默认不加锁）。"""
        assert str(workspace / "config.yml") in mgr._stamped

    def test_capability_report_is_honest_about_gaps(self, mgr):
        """能力矩阵必须如实报出 D 盘写边界这个挡不住的缺口。"""
        report = mgr.capability_report()
        assert "D 盘写入" in report
        assert "挡不住" in report
        assert "本次会话" in report


# ---------------------------------------------------------------- 能力实测

class TestSandboxCapabilities:
    def test_echo_roundtrip(self, mgr, workspace):
        p = _exec(mgr, "echo srt-it-ok", workspace)
        assert p.returncode == 0, _err(p)
        assert "srt-it-ok" in _out(p)

    def test_workspace_write_and_read_back(self, mgr, workspace):
        """工作区内可写（本机从 D:\\ 继承 M 权限，不需要 grant）。"""
        p = _exec(mgr, "echo written-by-sandbox > out.txt", workspace)
        assert p.returncode == 0, _err(p)
        assert (workspace / "out.txt").is_file()
        assert "written-by-sandbox" in (workspace / "out.txt").read_text(encoding="utf-8")

    def test_readable_file_is_readable(self, mgr, workspace):
        p = _exec(mgr, "type README.md", workspace)
        assert p.returncode == 0, _err(p)
        assert "readable content" in _out(p)

    def test_secret_file_read_is_denied(self, mgr, workspace):
        """机密文件读被 DENY ACE 拦住 —— 这是 stamp 的唯一目的。"""
        p = _exec(mgr, "type config.yml", workspace)
        assert p.returncode != 0, f"config.yml 竟然可读：{_out(p)}"
        assert _SECRET_TEXT not in _out(p)
        assert _SECRET_TEXT not in _err(p)

    def test_outbound_network_is_blocked(self, mgr, workspace):
        """WFP 按沙箱用户 SID 过滤 → 外联直接权限拒绝（不是超时）。"""
        code = "import socket;socket.create_connection(('1.1.1.1',80),3)"
        p = _exec(mgr, f'"{sys.executable}" -c "{code}"', workspace, timeout=60)
        assert p.returncode != 0, "外联竟然通了"
        assert "PermissionError" in _err(p) or "WinError" in _err(p)

    def test_loopback_network_is_blocked(self, mgr, workspace):
        """**回环也被拦** —— 这是 shot_web 的 CDP 路径无法进沙箱的硬依据。

        在宿主起一个 127.0.0.1 监听端口，沙箱内去连它：实测 WinError 10013。
        若哪天这条变绿（回环放行），tool_defs.py 里 shot_web 的「不收口」理由
        第 2 条需要重写。
        """
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def _accept_loop():
            srv.settimeout(0.5)
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                    conn.close()
                except Exception:
                    pass

        t = threading.Thread(target=_accept_loop, daemon=True)
        t.start()
        try:
            code = f"import socket;socket.create_connection(('127.0.0.1',{port}),3)"
            p = _exec(mgr, f'"{sys.executable}" -c "{code}"', workspace, timeout=60)
            assert p.returncode != 0, f"回环竟然通了（端口 {port}）"
            assert "PermissionError" in _err(p) or "WinError" in _err(p)
        finally:
            stop.set()
            t.join(timeout=2)
            srv.close()

    def test_git_works_in_sandbox_despite_foreign_owner(self, mgr, workspace):
        """GIT_CONFIG_* 注入 safe.directory 后，沙箱用户不再是 dubious ownership。"""
        p = _exec(mgr, "git rev-parse --is-inside-work-tree", workspace)
        assert "dubious ownership" not in _err(p).lower(), _err(p)
        assert p.returncode in (0, 128), _err(p)   # 128 = 不在仓库内，属正常


# ---------------------------------------------------------------- 端到端接线

class TestExecutorWiring:
    def test_executor_step_actually_runs_sandboxed(self, workspace):
        """走完整 Executor.run_step(sandbox=True) 通路，结果集须标 sandbox=True。"""
        from skill_engine.execution.executor import Executor

        ex = Executor(timeout=30)
        assert ex._sandbox_forced_off is False, "opt-in 状态下不该被测试环境压掉"
        res = ex.run_step("echo via-executor", cwd=workspace, sandbox=True)
        assert res["exit_code"] == 0, res["stderr"]
        assert "via-executor" in res["stdout"]
        assert res["sandbox"] is True

    def test_executor_denies_secret_read(self, workspace):
        """同一条通路下机密文件仍不可读（证明接线没把沙箱漏掉）。"""
        from skill_engine.execution.executor import Executor

        ex = Executor(timeout=30)
        res = ex.run_step("type config.yml", cwd=workspace, sandbox=True)
        assert res["exit_code"] != 0
        assert _SECRET_TEXT not in res["stdout"]

    def test_executor_report_after_use(self, workspace):
        from skill_engine.execution.executor import Executor

        ex = Executor(timeout=30)
        assert "未使用" in ex.sandbox_report()
        ex.run_step("echo x", cwd=workspace, sandbox=True)
        assert "本次会话" in ex.sandbox_report()
        ex.close_sandbox()
