"""
Executor — 命令执行器（沙箱），唯一 spawn 门神

所有命令执行都经过这里，V0.2 加 seccomp/landlock 只改一处。

对外暴露三个入口：
- run_preprocess(cmd, cwd) — 给 Assembler 用，宽松模式
- run_step(cmd, cwd, allowlist_override, timeout, sandbox) — 给 Runner / 工具用，
  可绑 skill 的 allowed-tools，沙箱形态走这里
- run_argv(argv, cwd, timeout) — 给库式只读工具用（argv 列表、不经 shell、不进沙箱），
  search_files 的 ripgrep 调用收口于此

**「唯一 spawn 门神」的适用范围**：凡是「跑一条 shell 命令」的路径必须走本类。
当前**有意排除**在外的只剩 shot_web 的 3 处 Edge 启动（GUI + 回环 CDP + %TEMP%
用户目录，理由与正解见 tool_defs.py 中 `_find_edge()` 上方的注释块）；
wsl_read_file / wsl_write_file 是历史遗留的裸 spawn，已无调用方
（见下方「WSL 遗留接口」段）。

安全措施：
1. 超时控制
2. PATH 限制
3. HOME 限制
4. 命令白名单（V0.2 引入，MVP 默认全允许）
5. 输出大小限制

MVP 阶段 allow_all=True，实际不检查白名单。
V0.2 改为 allow_all=False，DEFAULT_ALLOWLIST 生效。
"""

import subprocess
import sys
import os
import re
import locale
import shlex
import signal
import logging
from pathlib import Path
from typing import Optional

from .paths import to_native_path, native_path_hint

shell_quote = shlex.quote

# 自排除哨兵：_guard_self_kill 返回该值表示命令会显式杀死引擎自身，
# 调用方（_run）应直接拒绝执行，不交给 subprocess。
_SELF_KILL_REFUSE = "___SKILL_ENGINE_SELF_KILL_REFUSED___"


def _kill_process_tree(pid: int) -> None:
    """强杀进程树（含孙进程）。

    - Windows：taskkill /T /F（cmd → findstr 等子进程一并清除）；
    - POSIX：向进程组发 SIGKILL（配合 start_new_session 使用）。
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/pid", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


class Executor:
    """命令执行器（沙箱）— 唯一 spawn 门神

    使用方式：
    >>> executor = Executor(timeout=10)
    >>> result = executor.run_preprocess("python scripts/fetch_problem.py 49")
    >>> print(result["stdout"])
    """

    # 默认允许的命令（MVP 阶段宽松，实际 allowlist 不开）
    # V0.2 开 allowlist 时，rm/cp/mv 不该在默认名单里
    # 只保留纯读+fetch 类命令
    DEFAULT_ALLOWLIST = {
        "python", "python3",
        "cat", "echo", "curl", "git",
        "head", "tail", "wc",
        "mkdir", "touch",
    }

    MAX_OUTPUT_SIZE = 1024 * 1024  # 1MB

    def __init__(
        self,
        timeout: int = 10,
        allowlist: Optional[set[str]] = None,
        max_output: int = MAX_OUTPUT_SIZE,
        allow_all: bool = True,  # MVP 默认全允许
        shell: Optional[str] = None,  # None = 自动检测
        sandbox: Optional[bool] = None,  # None = 读 config.yml settings.sandbox.enabled
    ):
        self.timeout = timeout
        self.allowlist = allowlist or self.DEFAULT_ALLOWLIST
        self.max_output = max_output
        self.allow_all = allow_all  # MVP 设为 True，V0.2 改为 False
        # 自动检测 shell：Windows 优先用 WSL bash，否则 cmd；Linux/macOS 用 bash
        if shell is not None:
            self.shell = shell
        elif os.name == "nt":
            self.shell = self._detect_wsl_shell()
        else:
            self.shell = "bash"

        # Windows srt 沙箱（见 security/sandbox.py）。构造时只读配置，
        # 真正的会话级 ACL 成本在第一次需要进沙箱时由 SandboxManager 惰性付出——
        # 只跑只读白名单命令的会话完全不付这个成本。
        from skill_engine.config import get_sandbox_config
        self._sandbox_cfg = get_sandbox_config()
        self.sandbox_enabled = (
            bool(self._sandbox_cfg.get("enabled", True)) if sandbox is None else bool(sandbox)
        )
        self.sandbox_on_unavailable = str(
            self._sandbox_cfg.get("on_unavailable", "warn")
        ).strip().lower()
        # 测试环境强制关闭时作为「上限」：调用方显式传的 sandbox=True 也压掉
        # （bash 工具按路由传 True 会绕过默认值，导致单测付 10s 会话准备成本）。
        self._sandbox_forced_off = bool(self._sandbox_cfg.get("forced_off_by_test_env"))
        self._sandbox_mgr = None
        self._sandbox_warned = False

    @staticmethod
    def _detect_wsl_shell() -> str:
        """检测 shell 模式
        
        - 在 WSL 内部运行（WSL_DISTRO_NAME 存在）：用原生 bash
        - 在 Windows 上：用 cmd（即使 wsl.exe 可用也不用，文件应写入 Windows 文件系统）
        """
        if os.environ.get("WSL_DISTRO_NAME"):
            return "bash"
        return "cmd"

    @staticmethod
    def _to_wsl_path(win_path: str) -> str:
        """转换 Windows 路径为 WSL 路径：D:\\Code\\... → /mnt/d/code/..."""
        p = Path(win_path)
        drive = p.drive.lower().rstrip(":")
        rest = str(p.relative_to(p.anchor)).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"

    @staticmethod
    def _wsl_quote_path(path: str) -> str:
        """Quote 路径供 WSL bash 使用，保留 ~ 展开能力
        
        shlex.quote 会把 ~ 也包在单引号里导致 bash 不展开。
        这里把 ~ 部分单独保留不 quote，只 quote 后面的路径部分。
        """
        if path.startswith("~/"):
            rest = shlex.quote(path[2:])  # '.leetcode/docs/...'
            return f"~/{rest}"             # ~/'.leetcode/docs/...'
        elif path == "~":
            return "~"
        return shlex.quote(path)

    def run_preprocess(self, command: str, cwd: Path, multiline: bool = False) -> dict:
        """预处理型执行 — 给 Assembler 用

        宽松模式：不检查 allowlist（预处理阶段需要跑各种命令）
        但依然有 timeout / PATH / HOME 限制
        """
        return self._run(command, cwd=cwd, check_allowlist=False, multiline=multiline)

    def run_step(
        self,
        command: str,
        cwd: Path,
        allowlist_override: Optional[set[str]] = None,
        timeout: Optional[int] = None,
        sandbox: Optional[bool] = None,
    ) -> dict:
        """编排型执行 — 给 Runner 用

        严格模式：检查 allowlist（step exec 是 LLM 决定的，需要沙箱）

        Args:
            sandbox: True 强制进沙箱 / False 强制裸跑 / None 用 Executor 默认策略
                （config.yml ``settings.sandbox.enabled``，默认 True）。
                路由层（bash 工具 / run_python）按 decide_route() 显式传入。
        """
        allowlist = allowlist_override or self.allowlist
        return self._run(
            command, cwd=cwd, check_allowlist=True, allowlist=allowlist,
            timeout=timeout, sandbox=sandbox,
        )

    def run_argv(
        self,
        argv: list,
        cwd: Path,
        timeout: Optional[int] = None,
    ) -> dict:
        """以 argv 列表直接执行单个程序 —— 库式只读工具的 spawn 入口。

        与 ``run_step`` 的区别：不做字符串拼接、不经 shell 解析，所以 pattern /
        路径里的引号、空格、``&``、``$`` 不会被二次解释（这对「用户提供正则」的
        search_files 是硬要求：指令串化的那一刻就开始引入转义 bug）。
        ``search_files`` 的 ripgrep 调用走这里，使「唯一 spawn 门神」不被绕过。

        **刻意不提供沙箱形态**（无 ``sandbox`` 参数），两条实测依据：

        1. `srt-win exec` 确实支持 ``-- <TARGET>...`` 直接 argv 启动，但本机 ripgrep
           装在 ``C:\\Users\\<user>\\AppData\\Local\\Microsoft\\WinGet\\Packages\\…``
           下，沙箱用户读不到 —— 实测报
           ``CreateProcessAsUserW(rg.exe): 拒绝访问 (0x80070005)``；
           要放行只能把 WinGet 包目录加进 ``settings.sandbox.grant_read``，
           而那个目录的 ``(OI)(CI)`` 传播成本与收益完全不成比例。
        2. rg 是只读检索、无副作用，前面已用 ``--`` 终止了自身选项解析，
           沙箱并不能额外挡住什么（沙箱用户在 D 盘本来就能读全盘，见 sandbox.py 第 1 条）。

        需要隔离的命令请走 ``run_step(sandbox=True)``。
        """
        effective_timeout = timeout if timeout is not None else self.timeout
        native_cwd = to_native_path(cwd)
        if native_cwd is None or not native_cwd.is_dir():
            return {
                "stdout": "",
                "stderr": f"[工作目录无效: {cwd}] {native_path_hint(cwd)}",
                "exit_code": 1, "timed_out": False, "sandbox": False,
            }
        argv = [str(a) for a in (argv or [])]
        if not argv:
            return {
                "stdout": "", "stderr": "[空 argv]",
                "exit_code": 1, "timed_out": False, "sandbox": False,
            }
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd=str(native_cwd),
                env=self._build_env(native_cwd),
                start_new_session=(os.name != "nt"),
            )
            try:
                raw_out, raw_err = proc.communicate(timeout=effective_timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_tree(proc.pid)
                try:
                    raw_out, raw_err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    raw_out, raw_err = b"", b""
        except Exception as e:  # noqa: BLE001
            return {
                "stdout": "", "stderr": f"[异常: {str(e)}]",
                "exit_code": -1, "timed_out": False, "sandbox": False,
            }
        return {
            "stdout": self._decode(raw_out[:self.max_output]),
            "stderr": self._decode(raw_err[:self.max_output]),
            "exit_code": (-1 if timed_out else proc.returncode),
            "timed_out": timed_out,
            "sandbox": False,
        }

    @staticmethod
    def _decode(raw: bytes) -> str:
        """统一解码：先试 UTF-8，失败回退系统编码（Windows 中文输出走这条）。"""
        if not raw:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode(locale.getpreferredencoding(), errors="replace")

    def _run(
        self,
        command: str,
        cwd: Path,
        check_allowlist: bool = False,
        allowlist: Optional[set[str]] = None,
        multiline: bool = False,
        timeout: Optional[int] = None,
        sandbox: Optional[bool] = None,
    ) -> dict:
        """内部执行逻辑

        Args:
            command: 要执行的命令
            cwd: 工作目录
            check_allowlist: 是否检查白名单
            allowlist: 白名单集合
            multiline: 多行命令（代码块）
            timeout: 超时秒数（覆盖默认值）
        """
        effective_timeout = timeout if timeout is not None else self.timeout

        # 工作目录归一化：Windows 下 /d/x、/mnt/d/x 这类 POSIX 写法会让
        # subprocess 抛 [WinError 267] 目录名称无效，且异常被吞成 exit_code:-1，
        # 模型看不出根因只能反复重试。这里提前转换 + 校验，给出可执行的纠正提示。
        native_cwd = to_native_path(cwd)
        if native_cwd is None or not native_cwd.is_dir():
            return {
                "stdout": "",
                "stderr": (
                    f"[工作目录无效: {cwd}] {native_path_hint(cwd)}"
                    if native_cwd is None or not native_cwd.exists()
                    else f"[工作目录不是目录: {cwd}]"
                ),
                "exit_code": 1,
                "timed_out": False,
                "sandbox": False,
            }
        cwd = native_cwd

        # 安全检查
        if check_allowlist and not self.allow_all:
            if not self._is_safe(command, allowlist or self.allowlist):
                return {
                    "stdout": "",
                    "stderr": f"[安全拦截: 命令不在白名单中: {command}]",
                    "exit_code": 1,
                    "timed_out": False,
                    "sandbox": False,
                }

        # 防自杀护栏：阻止工具命令杀死 skill-engine 自身进程（MOA 进程内运行时的
        # 关键修复）。即便安全模式为 off，这道门神也始终生效。
        guarded = self._guard_self_kill(command)
        if guarded is _SELF_KILL_REFUSE:
            return {
                "stdout": "",
                "stderr": (
                    f"[安全拦截: 该命令会杀死 skill-engine 自身进程"
                    f"(PID {os.getpid()})，已拒绝执行。如需停止某服务，请改用其显式 PID]"
                ),
                "exit_code": 1,
                "timed_out": False,
                "sandbox": False,
            }
        command = guarded

        try:
            env = self._build_env(cwd)
            if self.shell == "wsl":
                # WSL bash：所有 Unix 路径语法直接工作
                # 用 wsl.exe --cd 设置工作目录，不出现在命令字符串中
                wsl_cwd = self._to_wsl_path(str(cwd))
                proc_args = ["wsl.exe", "--cd", wsl_cwd, "bash", "-c", command]
            elif self.shell == "cmd":
                # 参数列表方式（不用 shell=True）：避免外层 cmd 对命令字符串的
                # 二次引号解析——LLM 命令里的嵌套引号（如 "id=\""）曾导致内层
                # cmd 挂起，而 subprocess.run(timeout=) 只杀外层进程、杀不掉
                # 子进程树，communicate 永久死等（实测卡 30 分钟无输出）。
                proc_args = ["cmd.exe", "/c", command]
            else:
                proc_args = [self.shell, "-c", command]

            # 沙箱分支：把 proc_args 换成 `srt-win exec … <shell> -c <command>`。
            # 子进程不继承宿主 env → env 由 SandboxManager 全量经 --env 透传；
            # cwd 不接受参数 → 靠下面 Popen(cwd=…) 继承（实测子进程继承 broker cwd）。
            use_sandbox = self.sandbox_enabled if sandbox is None else bool(sandbox)
            if use_sandbox and self._sandbox_forced_off:
                use_sandbox = False
            ran_sandbox = False
            if use_sandbox:
                wrapped, sbox_err = self._wrap_sandbox(command, cwd, env)
                if wrapped is not None:
                    proc_args = wrapped
                    ran_sandbox = True
                elif sbox_err:
                    # fail-closed：srt 装了但这条命令起不来（stamp 失败/exec 报错），
                    # 绝不静默退化裸跑——设计文档 §4.7.9 的边界纪律。
                    return {
                        "stdout": "", "stderr": sbox_err, "exit_code": 1,
                        "timed_out": False, "sandbox": False,
                    }
                # sbox_err 为空 = 后端整体不可用且策略为 warn：已告警，按裸跑继续；
                # 此时 ran_sandbox 保持 False，observation 会如实标 sandbox: off。
            elif self.sandbox_enabled and sandbox is False:
                pass  # 路由层显式要求裸跑（只读白名单 / 已审批的删除/联网命令）

            proc = subprocess.Popen(
                proc_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,   # 切断 stdin：防 findstr 等命令等待输入挂起
                cwd=str(cwd),
                env=env,
                start_new_session=(os.name != "nt"),   # POSIX：独立进程组，超时可按组 kill
            )
            try:
                raw_out, raw_err = proc.communicate(timeout=effective_timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                # 超时：强杀整个进程树（Windows: taskkill /T /F；POSIX: killpg），
                # 否则子进程持有管道 → communicate 死等（旧实现的 30 分钟卡死根因）
                timed_out = True
                _kill_process_tree(proc.pid)
                try:
                    raw_out, raw_err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    raw_out, raw_err = b"", b""
                # 超时语义：exit_code=-1，stderr 前置超时提示（tool_dispatch 依赖）
                raw_err = f"[超时: {effective_timeout}s]".encode("utf-8", errors="replace") + raw_err

            # 手动解码：先试 UTF-8，失败回退到系统编码（与 run_argv 共用 _decode）
            stdout = self._decode(raw_out[:self.max_output] if raw_out else b"")
            stderr = self._decode(raw_err[:self.max_output] if raw_err else b"")

            return {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": (-1 if timed_out else proc.returncode),
                "timed_out": timed_out,
                "sandbox": ran_sandbox,
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": f"[异常: {str(e)}]",
                "exit_code": -1,
                "timed_out": False,
                "sandbox": False,
            }

    # ---------------------------------------------------------- WSL 遗留接口（无调用方）
    # wsl_read_file / wsl_write_file / _wsl_quote_path 已无任何调用方（全仓 grep 仅本文件
    # 命中；文件读写统一走 read_file / write_file handler，shell=wsl 的通路只在 _run 里）。
    # 保留而不删的原因：属于 Executor 的历史公开接口，外部（skill 脚本）可能直接引用；
    # **新代码不要再使用** —— 它们是绕过唯一 spawn 门神的裸 subprocess.run，
    # 既不进沙箱也不受超时/进程树清理/自排除护栏管辖。确认无外部依赖后建议整体删除。

    def wsl_read_file(self, path: str) -> str:
        """通过 WSL bash 读取文件（处理 WSL 绝对路径和 ~ 路径）
        
        Args:
            path: WSL 路径（如 /home/andre/.leetcode/docs/题解.md 或 ~/.leetcode/...）
            
        Returns:
            文件内容字符串
            
        Raises:
            FileNotFoundError: 文件不存在
        """
        dest = self._wsl_quote_path(path)
        result = subprocess.run(
            ["wsl.exe", "bash", "-c", f"cat {dest}"],
            capture_output=True, timeout=self.timeout,
        )
        if result.returncode != 0:
            raise FileNotFoundError(f"WSL path not found: {path}")
        return result.stdout.decode("utf-8", errors="replace")

    def wsl_write_file(self, path: str, content: str) -> None:
        """通过 WSL bash 写入文件（处理 WSL 绝对路径和 ~ 路径）
        
        Args:
            path: WSL 路径（如 /home/andre/.leetcode/docs/题解.md 或 ~/.leetcode/...）
            content: 文件内容
            
        Raises:
            IOError: 写入失败
        """
        import base64, os
        encoded = base64.b64encode(content.encode("utf-8")).decode()
        dest = self._wsl_quote_path(path)
        # 在 Python 端计算目录路径，避免 bash 中 $(dirname) 的单词分割问题
        dir_path = self._wsl_quote_path(os.path.dirname(path))
        cmd = f"mkdir -p {dir_path} && echo {encoded} | base64 -d > {dest}"
        result = subprocess.run(
            ["wsl.exe", "bash", "-c", cmd],
            capture_output=True,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            raise IOError(f"WSL write failed: {result.stderr.decode('utf-8', errors='replace')}")

    # ---------------------------------------------------------------- 沙箱

    def _wrap_sandbox(self, command: str, cwd: Path, env: dict):
        """把命令包装成 srt 沙箱 argv。

        Returns:
            (argv, "")      —— 包装成功，用 argv 启动（必须 Popen(cwd=cwd)）
            (None, err)     —— fail-closed：srt 可用但本次包装/准备失败，调用方拒绝执行
            (None, "")      —— 后端整体不可用且 ``on_unavailable=warn``，已告警，按裸跑继续
        """
        from skill_engine.security.sandbox import SandboxManager

        if self.shell not in ("cmd", "bash"):
            # WSL 形态没有对应的 srt 启动路径；属「后端不支持」而非「命令失败」
            reason = f"shell={self.shell!r} 不支持沙箱（srt-win 只能启动 cmd/bash）"
            if self.sandbox_on_unavailable == "block":
                return None, f"[沙箱不可用，已拒绝执行] {reason}"
            self._warn_unavailable(reason)
            return None, ""

        try:
            if self._sandbox_mgr is None:
                self._sandbox_mgr = SandboxManager.get(
                    cwd,
                    deny_read=self._sandbox_cfg.get("deny_read") or [],
                    grant_read=self._sandbox_cfg.get("grant_read") or [],
                    srt_bin=self._sandbox_cfg.get("srt_bin") or None,
                    env_passthrough=self._sandbox_cfg.get("env_passthrough") or [],
                )
            mgr = self._sandbox_mgr
            if not mgr.ensure_ready():
                reason = mgr.last_error or "沙箱后端不可用"
                if self.sandbox_on_unavailable == "block":
                    return None, f"[沙箱不可用，已拒绝执行] {reason}"
                self._warn_unavailable(reason)
                return None, ""
            argv = mgr.wrap(command, cwd=cwd, env=env, shell=self.shell)
            if argv is None:
                # 已 ready 却包装失败 = 运行期错误 → 一律 fail-closed
                return None, (
                    f"[沙箱不可用，已拒绝执行] {mgr.last_error or '包装命令失败'}\n"
                    "该命令若确实需要真实执行环境，请单独一步由用户显式批准后裸跑，"
                    "不要期待这里自动降级。"
                )
            return argv, ""
        except Exception as e:  # noqa: BLE001
            # 沙箱接入自身的异常同样 fail-closed：宁可拒绝，不可静默裸跑
            return None, f"[沙箱不可用，已拒绝执行] 沙箱接入异常：{type(e).__name__}: {e}"

    def _warn_unavailable(self, reason: str) -> None:
        """后端不可用且策略为 warn：只告警一次，避免每步刷屏。"""
        if self._sandbox_warned:
            return
        self._sandbox_warned = True
        logging.getLogger("skill_engine.sandbox").warning(
            "沙箱后端不可用，本次运行按裸跑继续（settings.sandbox.on_unavailable=warn）：%s",
            reason,
        )
        print(f"     [sandbox] 未启用，按裸跑继续：{reason[:120]}")

    def sandbox_report(self) -> str:
        """沙箱能力/使用情况报告（供 doctor 与 debug 轨迹）。"""
        if self._sandbox_mgr is None:
            return "沙箱：本次运行未使用（无需要进沙箱的命令）"
        return self._sandbox_mgr.capability_report()

    def close_sandbox(self) -> None:
        """release 沙箱 ACE（幂等；进程退出时 SandboxManager 还会 atexit 兜底）。"""
        if self._sandbox_mgr is not None:
            self._sandbox_mgr.close()

    def _guard_self_kill(self, command: str) -> str:
        """防止工具命令杀死 skill-engine 自身进程（MOA 进程内运行时的自杀护栏）。

        Windows: 任一 ``taskkill`` 都注入 ``/FI "PID ne <引擎PID>"``，使引擎自身
            进程被镜像名匹配排除；过滤器紧贴 ``taskkill`` 关键字之后插入，命令其余
            部分（含引号内的 ``&&``、``&`` 分隔段）完全不动。
        POSIX: ``kill <引擎PID>``（显式自杀）返回哨兵让调用方拒绝；``pkill``/``killall
            <名称>`` 改写为 ``pgrep`` 循环，跳过引擎 PID 后再 kill。

        返回改写后的命令；若属显式自杀则返回模块级哨兵 ``_SELF_KILL_REFUSE``。
        """
        pid = os.getpid()
        if os.name == "nt":
            # 仅当命令含 taskkill 且尚未注入过自排除过滤器时才改写
            if re.search(r"(?i)\btaskkill\b", command) and f"PID ne {pid}" not in command:
                command = re.sub(
                    r"(?i)\btaskkill\b",
                    f'taskkill /FI "PID ne {pid}"',
                    command,
                )
            return command

        # POSIX
        if re.search(r"(?i)\bkill\b", command):
            # 显式自杀：kill [-signal] <引擎PID>
            if re.search(r"(?i)\bkill\b(?:\s+-\w+)?\s+" + str(pid) + r"\b", command):
                return _SELF_KILL_REFUSE
        if re.search(r"(?i)\b(?:pkill|killall)\b", command):
            def _rew(m):
                sig = m.group(2) or ""
                pat = m.group(3) or ""
                return (
                    f'for p in $(pgrep {pat} 2>/dev/null); '
                    f'do [ "$p" -ne {pid} ] && kill {sig} "$p"; done'
                )
            command = re.sub(
                r"(?i)\b(?:pkill|killall)\b(\s+(-\w+))?(\s+(\S+))?",
                _rew, command,
            )
        return command

    def _is_safe(self, command: str, allowlist: set[str]) -> bool:
        """检查命令是否安全（白名单检查）

        注意：V0.2 要吃 CC 的 allowed-tools 语法
        Bash(git *) / Bash(python scripts/*)
        目前 MVP 只扫 basename
        """
        cmd_parts = command.strip().split()
        if not cmd_parts:
            return False

        cmd_name = Path(cmd_parts[0]).name
        return cmd_name in allowlist

    def _build_env(self, cwd: Path) -> dict:
        """构建沙箱环境变量。

        整改 A3a：自动探测 cwd 下的虚拟环境（.venv / venv），并把其 site-packages
        注入 PYTHONPATH、把 venv 的 python 前置到 PATH、设 VIRTUAL_ENV。这样子进程
        `python -c "import xxx"` 能直接找到项目依赖，无需退化为 write_file + bash 连环
        （trace 实证：环境未预置导致 6 次 python -c 失败、7 个临时 .py 脚本）。
        """
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["LANG"] = "C.UTF-8"
        # 不覆盖 HOME，保留系统真实用户目录
        sep = ";" if os.name == "nt" else ":"
        env["PATH"] = f"{cwd}/scripts{sep}{cwd}{sep}{env.get('PATH', '/usr/bin:/bin')}"

        # 整改 A3a：注入项目 Python 环境（venv 优先，回退当前解释器 sys.path）
        site_paths: list[str] = []
        venv_dir = None
        for cand in (".venv", "venv"):
            vd = cwd / cand
            if vd.is_dir():
                venv_dir = vd
                break
        if venv_dir is not None:
            # Windows: venv/Scripts/python.exe；POSIX: venv/bin
            bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
            if bin_dir.is_dir():
                env["PATH"] = f"{bin_dir}{sep}{env['PATH']}"
            env["VIRTUAL_ENV"] = str(venv_dir)
            # 主动把 venv site-packages 加进 PYTHONPATH（双保险，覆盖 VIRTUAL_ENV 未被识别的情况）
            sp = self._find_site_packages(venv_dir)
            if sp:
                site_paths.append(sp)
        else:
            # 无 venv：用当前解释器的 sys.path，保证 python -c 与引擎同环境
            import sys as _sys
            site_paths.extend(p for p in _sys.path if p and Path(p).is_dir())
        if site_paths:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = sep.join([*site_paths, existing]) if existing else sep.join(site_paths)
        return env

    @staticmethod
    def _find_site_packages(venv_dir: Path) -> str:
        """定位 venv 的 site-packages 目录（Windows: Lib/site-packages；POSIX: lib/pythonX.Y/site-packages）。"""
        # Windows
        win_sp = venv_dir / "Lib" / "site-packages"
        if win_sp.is_dir():
            return str(win_sp)
        # POSIX：lib/pythonX.Y/site-packages
        import glob as _glob
        matches = _glob.glob(str(venv_dir / "lib" / "python*" / "site-packages"))
        if matches and Path(matches[0]).is_dir():
            return matches[0]
        return ""
