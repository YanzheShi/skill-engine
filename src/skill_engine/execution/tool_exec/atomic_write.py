"""写入暂存：先写 *.partial，完整性通过才原子转正，避免"半成品转正"。

开关：SKILLS_ENGINE_ATOMIC_WRITE=0/off/false/no 关闭（回到直接写目标文件）。
"""

import os
from pathlib import Path


def enabled() -> bool:
    return os.environ.get("SKILLS_ENGINE_ATOMIC_WRITE", "1").strip().lower() not in (
        "0", "off", "false", "no",
    )


def partial_path(full_path: Path) -> Path:
    return full_path.with_name(full_path.name + ".partial")


def is_complete(verdict: str) -> bool:
    """完整度回执 → 是否可转正。只有明确 complete: NO 才阻止。"""
    return not str(verdict or "").startswith("complete: NO")


def should_finalize(verdict: str, truncated: bool = False) -> bool:
    """是否允许把 partial 转正。

    - ``complete: yes``（HTML 闭合 / JSON 可解析）→ 可转正，即使本轮被截断。
    - ``complete: NO`` → 不转正。
    - ``complete: unknown``（如 .py/.md，无法廉价判定）→ 本轮被截断时**不转正**，
      避免把半截代码写成正式文件；正常轮次照常转正。
    """
    v = str(verdict or "")
    if v.startswith("complete: NO"):
        return False
    if v.startswith("complete: yes"):
        return True
    return not truncated


def finalize(partial: Path, target: Path) -> None:
    """同目录 os.replace，单盘原子。"""
    os.replace(partial, target)
