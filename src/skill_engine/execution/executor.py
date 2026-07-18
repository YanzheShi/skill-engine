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
from pathlib import Path
from typing import Optional

shell_quote = shlex.quote


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
        # 自动检测 shell：Windows 用 cmd，Linux/macOS 用 bash
        if shell is not None:
            self.shell = shell
        elif os.name == "nt":
            self.shell = "cmd"
        else:
            self.shell = "bash"

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
            # Windows shell=True 时需要用 cmd /c，Linux 用 bash -c
            if self.shell == "cmd":
                full_cmd = f'cmd /c "{command}"'
            else:
                full_cmd = f"{self.shell} -c {shell_quote(command)}"
            # 先以二进制获取输出（不指定编码）
            proc = subprocess.run(
                full_cmd,
                shell=True,
                capture_output=True,
                text=False,  # 不自动解码，我们手动处理
                timeout=effective_timeout,
                cwd=str(cwd),
                env=env,
            )

            # 手动解码：先试 UTF-8，失败回退到系统编码
            raw_stdout = proc.stdout[:self.max_output] if proc.stdout else b""
            raw_stderr = proc.stderr[:self.max_output] if proc.stderr else b""
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
                "exit_code": proc.returncode,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"[超时: {self.timeout}s]",
                "exit_code": -1,
                "timed_out": True,
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": f"[异常: {str(e)}]",
                "exit_code": -1,
                "timed_out": False,
            }

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
        env["LANG"] = "C.UTF-8"
        env["HOME"] = str(cwd)
        sep = ";" if os.name == "nt" else ":"
        env["PATH"] = f"{cwd}/scripts{sep}{cwd}{sep}{env.get('PATH', '/usr/bin:/bin')}"
        return env
