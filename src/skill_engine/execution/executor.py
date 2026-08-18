"""
Executor — 命令执行器（沙箱），唯一 spawn 门神

所有命令执行都经过这里，V0.2 加 seccomp/landlock 只改一处。

对外暴露两个入口：
- run_preprocess(cmd, cwd) — 给 Assembler 用，宽松模式
- run_step(cmd, cwd, allowlist_override) — 给 Runner 用，可绑 skill 的 allowed-tools

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
import locale
import shlex
import signal
from pathlib import Path
from typing import Optional

from .paths import to_native_path, native_path_hint

shell_quote = shlex.quote


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

    def run_step(self, command: str, cwd: Path, allowlist_override: Optional[set[str]] = None, timeout: Optional[int] = None) -> dict:
        """编排型执行 — 给 Runner 用

        严格模式：检查 allowlist（step exec 是 LLM 决定的，需要沙箱）
        """
        allowlist = allowlist_override or self.allowlist
        return self._run(command, cwd=cwd, check_allowlist=True, allowlist=allowlist, timeout=timeout)

    def _run(
        self,
        command: str,
        cwd: Path,
        check_allowlist: bool = False,
        allowlist: Optional[set[str]] = None,
        multiline: bool = False,
        timeout: Optional[int] = None,
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
                }

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

            # 手动解码：先试 UTF-8，失败回退到系统编码
            raw_stdout = raw_out[:self.max_output] if raw_out else b""
            raw_stderr = raw_err[:self.max_output] if raw_err else b""
            try:
                stdout = raw_stdout.decode("utf-8")
            except UnicodeDecodeError:
                stdout = raw_stdout.decode(locale.getpreferredencoding(), errors="replace")
            try:
                stderr = raw_stderr.decode("utf-8")
            except UnicodeDecodeError:
                stderr = raw_stderr.decode(locale.getpreferredencoding(), errors="replace")

            return {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": (-1 if timed_out else proc.returncode),
                "timed_out": timed_out,
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": f"[异常: {str(e)}]",
                "exit_code": -1,
                "timed_out": False,
            }

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
        """构建沙箱环境变量"""
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["LANG"] = "C.UTF-8"
        # 不覆盖 HOME，保留系统真实用户目录
        # env["HOME"] = str(cwd)  # 已删除：子进程 HOME 应指向用户目录，而非 skill 目录
        sep = ";" if os.name == "nt" else ":"
        env["PATH"] = f"{cwd}/scripts{sep}{cwd}{sep}{env.get('PATH', '/usr/bin:/bin')}"
        return env
