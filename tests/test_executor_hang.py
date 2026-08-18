"""Executor 卡死回归测试（Windows）。

背景：LLM 生成含嵌套引号的命令（如 findstr /c:"id=\\"）经旧实现的
`cmd /c "..."` + shell=True 包装后，内层 cmd 解析错乱挂起；而
subprocess.run(timeout=) 超时只杀外层进程、杀不掉子进程树，
communicate 永久死等——实测 MOA 卡 30 分钟无任何输出。
修复：Popen 参数列表 + stdin=DEVNULL + 超时强杀进程树。
"""

import sys
import time
from pathlib import Path

import pytest

from skill_engine.execution.executor import Executor

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="嵌套 cmd 卡死场景仅 Windows")


def _ex(timeout=10):
    return Executor(timeout=timeout, allow_all=True)


def test_nested_quote_cmd_returns_quickly():
    """用户现场卡死的那类命令（嵌套引号）必须快速返回，不得挂起。"""
    ex = _ex()
    cmd = 'cd /d D:\\Code\\PycharmProjects\\test_tmp1 && findstr /c:"id=\\"" index.html && echo done'
    t0 = time.time()
    r = ex.run_step(cmd, cwd=Path(r"D:\Code\PycharmProjects\test_tmp1"), timeout=5)
    dt = time.time() - t0
    assert dt < 10, f"嵌套引号命令挂起 {dt:.1f}s"
    assert r["timed_out"] is False


def test_nested_cmd_timeout_kills_tree():
    """双层 cmd + 长命令：3s 超时必须强杀整个进程树并返回，不得死等。"""
    ex = _ex()
    cmd = 'cmd /c powershell -Command Start-Sleep -Seconds 30'
    t0 = time.time()
    r = ex.run_step(cmd, cwd=Path(r"D:\Code\PycharmProjects\skill-engine"), timeout=3)
    dt = time.time() - t0
    assert r["timed_out"] is True, "应标记超时"
    assert dt < 20, f"kill 进程树后 communicate 死等 {dt:.1f}s"
    assert r["exit_code"] == -1
    assert "[超时" in r["stderr"]


def test_stdin_devnull_findstr_no_hang():
    """findstr 无参数会读 stdin：stdin=DEVNULL 后必须立即返回而非等待输入。"""
    ex = _ex()
    t0 = time.time()
    r = ex.run_step("findstr", cwd=Path(r"D:\Code\PycharmProjects\skill-engine"), timeout=5)
    dt = time.time() - t0
    assert dt < 10, f"findstr 等待 stdin 挂起 {dt:.1f}s"


def test_normal_command_still_works():
    ex = _ex()
    r = ex.run_step("echo hi", cwd=Path(r"D:\Code\PycharmProjects\skill-engine"), timeout=5)
    assert r["exit_code"] == 0
    assert r["stdout"].strip() == "hi"