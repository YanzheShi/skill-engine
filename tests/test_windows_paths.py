"""Windows 路径与 shell 环境适配测试

背景：用户在 Git Bash 里执行
    skill-engine session -s code-builder -w /d/Code/PycharmProjects/demo-project
每条 bash 命令都报 [WinError 267] 目录名称无效——因为引擎跑的是 Windows 原生
Python，`/d/...` 不是合法目录。异常还被吞成 exit_code:-1，模型看不出根因，
只能一路瞎试到迭代上限。

本文件锁住四层修复：
1. posix_to_windows / to_native_path —— 路径归一化（纯函数，跨平台可测）
2. Executor —— 无效 cwd 返回可读错误，而不是裸 WinError
3. build_env_header —— 把 OS / shell / 工作目录显式告诉 LLM
4. format_observation —— 已知失败模式补一行可执行 hint
"""

import os
from pathlib import Path

import pytest

from skill_engine.execution.paths import (
    posix_to_windows,
    to_native_path,
    native_path_hint,
)
from skill_engine.execution.tool_dispatch import (
    build_env_header,
    format_observation,
    _diagnose_shell_error,
    _resolve_path,
)
from skill_engine.execution.executor import Executor


# ---------------------------------------------------------------- 路径归一化

@pytest.mark.parametrize("raw,expected", [
    ("/d/Code/PycharmProjects/demo-project", r"D:\Code\PycharmProjects\demo-project"),
    ("/c/Users/Andre", r"C:\Users\Andre"),
    ("/mnt/d/Code/proj", r"D:\Code\proj"),
    ("/cygdrive/e/data", r"E:\data"),
    ("/d", "D:\\"),
    ("D:/Code/proj", r"D:\Code\proj"),
    (r"D:\Code\proj", r"D:\Code\proj"),
])
def test_posix_to_windows_conversion_table(raw, expected):
    assert posix_to_windows(raw) == expected


@pytest.mark.parametrize("raw", [
    "./src",
    "src/demo/main.py",
    "/usr/local/bin",      # 多字符首段，不是盘符，不猜
    "/srv",
    "",
])
def test_posix_to_windows_leaves_others_untouched(raw):
    assert posix_to_windows(raw) == raw


def test_to_native_path_expands_home():
    p = to_native_path("~")
    assert p is not None
    assert "~" not in str(p)


def test_to_native_path_none_and_empty():
    assert to_native_path(None) is None
    assert to_native_path("") is None


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 需要盘符转换")
def test_to_native_path_converts_git_bash_style_on_windows(tmp_path):
    # 用真实存在的目录构造 Git Bash 写法，验证转换后确实指向同一目录
    native = tmp_path.resolve()
    drive = str(native)[0].lower()
    rest = str(native)[3:].replace("\\", "/")
    git_bash = f"/{drive}/{rest}"
    converted = to_native_path(git_bash)
    assert converted is not None
    assert converted.is_dir()
    assert converted.resolve() == native


def test_native_path_hint_suggests_native_form():
    hint = native_path_hint("/d/Code/proj")
    assert "D:" in hint


# ------------------------------------------------------------- Executor cwd

def test_executor_invalid_cwd_returns_readable_error():
    """无效 cwd 不再抛裸 WinError，而是给出可执行提示。"""
    ex = Executor(timeout=5)
    result = ex.run_step("echo hello", cwd=Path("/d/definitely/not/here"))
    assert result["exit_code"] == 1
    assert "工作目录无效" in result["stderr"]
    assert "D:" in result["stderr"]          # 含纠正写法
    assert not result["timed_out"]


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows 下 Git Bash 路径可直接使用")
def test_executor_accepts_git_bash_cwd_on_windows(tmp_path):
    """传 /d/... 风格 cwd 时自动归一化，命令照常执行。"""
    native = tmp_path.resolve()
    drive = str(native)[0].lower()
    rest = str(native)[3:].replace("\\", "/")
    ex = Executor(timeout=15)
    result = ex.run_step("echo ok", cwd=Path(f"/{drive}/{rest}"))
    assert result["exit_code"] == 0
    assert "ok" in result["stdout"]


def test_executor_valid_cwd_still_works(tmp_path):
    ex = Executor(timeout=15)
    result = ex.run_step("echo ok", cwd=tmp_path)
    assert result["exit_code"] == 0
    assert "ok" in result["stdout"]


# ------------------------------------------------------------- env 环境头

def test_env_header_contains_working_dir_and_shell():
    header = build_env_header(Path(r"D:\Code\demo"), shell="cmd")
    assert "<env>" in header
    assert r"D:\Code\demo" in header
    assert "cmd.exe" in header
    # 关键约定必须在场
    assert "不需要也不要 cd" in header
    assert "/mnt/d/" in header          # 明确点名禁止的写法


def test_env_header_cmd_lists_windows_command_equivalents():
    header = build_env_header(Path(r"D:\x"), shell="cmd")
    for token in ("dir", "type", "findstr"):
        assert token in header


def test_env_header_bash_does_not_warn_about_cmd():
    # 注意用 str(Path(...))：Windows 上 Path("/home/u/proj") 渲染成 \home\u\proj，
    # 直接断言字面量会在跨平台跑时假失败。
    base = Path("/home/u/proj")
    header = build_env_header(base, shell="bash")
    assert "cmd.exe" not in header
    assert str(base) in header
    assert "Shell: bash" in header


def test_env_header_prepended_to_prompt_is_short_enough():
    """环境头是每轮都进上下文的固定开销，控制在 1000 字符内。"""
    header = build_env_header(Path(r"D:\Code\demo"), shell="cmd")
    assert len(header) < 1000


# --------------------------------------------------- observation 失败提示

def test_diagnose_winerror_267():
    hint = _diagnose_shell_error("[异常: [WinError 267] 目录名称无效。]")
    assert "Git Bash" in hint or "/d/" in hint


def test_diagnose_command_not_found_cmd():
    hint = _diagnose_shell_error("'ls' 不是内部或外部命令，也不是可运行的程序")
    assert "cmd.exe" in hint


def test_diagnose_unknown_error_returns_empty():
    assert _diagnose_shell_error("some random failure") == ""
    assert _diagnose_shell_error("") == ""


def test_format_observation_appends_hint_on_known_failure():
    obs = format_observation(
        "cd /d/Code/proj && ls -la",
        {"stdout": "", "stderr": "[异常: [WinError 267] 目录名称无效。]",
         "exit_code": -1, "timed_out": False},
    )
    assert "hint:" in obs
    assert "exit_code: -1" in obs


def test_format_observation_no_hint_on_success():
    obs = format_observation(
        "echo ok",
        {"stdout": "ok", "stderr": "", "exit_code": 0, "timed_out": False},
    )
    assert "hint:" not in obs


# ------------------------------------------------------- _resolve_path 归一化

@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 需要盘符转换")
def test_resolve_path_handles_git_bash_absolute_on_windows(tmp_path):
    """模型给 /d/... 路径时不能被当成相对路径拼到 base_dir 后面。"""
    resolved = _resolve_path("/d/Code/x/main.py", tmp_path)
    assert str(resolved).startswith("D:")
    assert str(tmp_path) not in str(resolved)


def test_resolve_path_relative_still_joins_base(tmp_path):
    resolved = _resolve_path("src/demo/main.py", tmp_path)
    assert str(resolved).startswith(str(tmp_path))
