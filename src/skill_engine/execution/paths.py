"""跨平台路径归一化

痛点：Windows 用户习惯在 Git Bash / WSL 里敲命令，于是会把
`/d/Code/proj`（Git Bash）或 `/mnt/d/Code/proj`（WSL）这种 POSIX 风格路径
直接传给 `-w/--working-root`。但引擎本身跑的是 Windows 原生 Python，
`subprocess(cwd="/d/Code/proj")` 会直接抛 `[WinError 267] 目录名称无效`，
异常还会被吞成 `exit_code: -1`，模型完全看不出问题在哪，只能反复瞎试。

本模块把这类路径统一转成原生形式，让"在 Windows 上就走 Windows 目录"成为默认行为。
"""

import os
import re
from pathlib import Path
from typing import Optional, Union

# /d/Code/x  或  /d
_GIT_BASH_DRIVE = re.compile(r"^/([A-Za-z])(/.*)?$")
# /mnt/d/Code/x
_WSL_DRIVE = re.compile(r"^/mnt/([A-Za-z])(/.*)?$")
# /cygdrive/d/Code/x
_CYGWIN_DRIVE = re.compile(r"^/cygdrive/([A-Za-z])(/.*)?$")


def posix_to_windows(path: str) -> str:
    """把 POSIX 风格的 Windows 路径转成原生形式（纯函数，不依赖当前 OS）。

    转换表::

        /d/Code/proj        -> D:\\Code\\proj
        /mnt/d/Code/proj    -> D:\\Code\\proj
        /cygdrive/d/proj    -> D:\\proj
        /d                  -> D:\\
        D:/Code/proj        -> D:\\Code\\proj   (分隔符统一)
        ./src               -> ./src            (原样返回)

    无法识别的路径原样返回，绝不猜测。
    """
    if not path:
        return path

    s = path.replace("\\", "/")

    for pattern in (_WSL_DRIVE, _CYGWIN_DRIVE, _GIT_BASH_DRIVE):
        m = pattern.match(s)
        if m:
            drive = m.group(1).upper()
            rest = (m.group(2) or "").lstrip("/")
            return f"{drive}:\\{rest}".replace("/", "\\") if rest else f"{drive}:\\"

    # 已经是盘符形式，只统一分隔符
    if re.match(r"^[A-Za-z]:[/\\]", s):
        return s.replace("/", "\\")

    return path


def to_native_path(path: Optional[Union[str, Path]]) -> Optional[Path]:
    """把用户/LLM 传入的路径转成当前平台可用的 Path。

    - 任意平台：展开 ``~``
    - 仅 Windows：额外把 ``/d/x`` ``/mnt/d/x`` ``/cygdrive/d/x`` 转成 ``D:\\x``

    在 Linux/macOS 上 ``/mnt/d/x`` 是合法真实路径，因此不做任何转换。

    Args:
        path: 原始路径，None 时返回 None

    Returns:
        归一化后的 Path，或 None
    """
    if path is None:
        return None
    s = str(path)
    if not s:
        return None
    s = os.path.expanduser(s)
    if os.name == "nt":
        s = posix_to_windows(s)
    return Path(s)


def native_path_hint(original: Union[str, Path]) -> str:
    """为无效路径生成一句可执行的纠正提示（给用户和 LLM 看）。"""
    guess = posix_to_windows(str(original))
    if guess != str(original):
        return f"在 Windows 上请改用原生写法：{guess}"
    return "在 Windows 上请使用 D:\\path\\to\\dir 或 D:/path/to/dir 形式的路径"


def runtime_dir(working_root: Optional[Union[str, Path]] = None) -> Path:
    """返回引擎运行时产物目录 ``<working_root>/.skill-engine``，并自动建目录。

    用途：把引擎运行时散落生成的产物（文件快照、session 状态、pastes、
    截图、debug 日志、MOA 检查点）统一收口到该子目录，避免污染被操作的
    项目目录。所有下游写入点从此处取根，避免路径拼接散落重复。

    兼容性：
    - ``working_root`` 应为入口处经 ``to_native_path()`` 归一化后的 native 路径
      （如 ``D:/Code/proj``）。此处**不再**做 POSIX→Windows 二次转换，直接拼接，
      避免把已正确的路径破坏（保持幂等）。
    - ``working_root`` 为 None 时回退到进程 cwd（与既有 ``Path.cwd()`` 行为一致）。
    - ``.skill-engine`` 在 Windows / Linux 均为合法目录名；``mkdir`` 双平台正常。

    Args:
        working_root: 工作目录（native 形式）。None → 用 ``Path.cwd()``。

    Returns:
        已确保存在的 ``Path``（``<working_root>/.skill-engine``）。
    """
    base = Path(working_root) if working_root else Path.cwd()
    d = base / ".skill-engine"
    d.mkdir(parents=True, exist_ok=True)
    return d
