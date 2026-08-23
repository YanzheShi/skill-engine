"""粘贴内容外置（Hermes-lite）

交互式会话里用户粘贴的大段文本（日志、报错、代码块）会被整段捕获
（bracketed paste 负责不被终端按 \\n 拆成多条命令），但这里再进一步：
超过阈值的内容落盘到临时文件，返回一个紧凑引用 token；提交时由 runner
把 token 解析成"请读此文件"的指令交给 agent。

这样既能整段粘贴成一条指令，又不会把上千行原文内联进 agent 的 prompt。
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from skill_engine.execution.paths import runtime_dir

# 默认落盘目录兜底：REPL 启动时会显式传 base=working_root/.skill-engine/pastes，
# 故此处仅在无任何 base 时惰性解析为 <cwd>/.skill-engine/pastes（不污染项目目录）。
# 早期版本默认落到系统 temp（tempfile.gettempdir()/"skill_engine_pastes"），改为
# 跟随工作目录的 .skill-engine 子目录，使产物集中、可清理。
DEFAULT_PASTE_DIR = None

# 触发外置的阈值：行数或字符数超过任意一个即落盘
MIN_LINES = 3
MIN_CHARS = 500

# [Pasted text #1: 8 lines → D:\...\paste_1_123456.txt]
_TOKEN_RE = re.compile(r"\[Pasted text #(\d+): (\d+) lines → (.+?)\]")


def set_paste_dir(path) -> None:
    """覆盖默认落盘目录（REPL 传 working_root 时用）。"""
    global DEFAULT_PASTE_DIR
    DEFAULT_PASTE_DIR = Path(path)


def save_paste(content: str, base: Path | None = None) -> str | None:
    """内容超过阈值则落盘并返回引用 token；否则返回 None（保持原文）。"""
    if not content:
        return None
    lines = content.splitlines()
    if len(lines) < MIN_LINES and len(content) < MIN_CHARS:
        return None
    base = Path(base) if base else (DEFAULT_PASTE_DIR or (runtime_dir() / "pastes"))
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    ts = datetime.now().strftime("%H%M%S")
    idx = _next_index(base)
    path = base / f"paste_{idx}_{ts}.txt"
    try:
        path.write_text(content, encoding="utf-8")
    except Exception:
        return None
    _maybe_cleanup(base, keep=50)
    return f"[Pasted text #{idx}: {len(lines)} lines → {path}]"


def resolve_refs(text: str) -> Tuple[str, List[Path]]:
    """把引用 token 替换成可读提示，并返回所有被引用的文件路径。"""
    paths: List[Path] = []

    def repl(m):
        p = Path(m.group(3).strip())
        paths.append(p)
        return f"（用户粘贴内容已保存至 {p}，请用文件读取工具查看）"

    cleaned = _TOKEN_RE.sub(repl, text)
    return cleaned, paths


def _next_index(base: Path) -> int:
    try:
        existing = list(base.glob("paste_*.txt"))
    except Exception:
        existing = []
    return len(existing) + 1


def _maybe_cleanup(base: Path, keep: int = 50) -> None:
    """落盘文件超过 keep 个时，删最旧的，避免无限增长。"""
    try:
        files = sorted(base.glob("paste_*.txt"), key=lambda p: p.stat().st_mtime)
        for old in files[:-keep]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass
