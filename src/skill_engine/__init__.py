"""
Skills Engine — 独立的 skills 解析和路由工具

兼容 Claude Code Agent Skills 开放标准的核心子集。

使用方式：
    skill-engine list          # 列出所有 skills
    skill-engine match "query" # 匹配 skills
    skill-engine run "query"   # 执行 skill
"""

__version__ = "0.1.0"

import sys as _sys


def ensure_utf8_io() -> None:
    """CLI 输出统一 UTF-8（幂等）。

    stdout 重定向到文件时 Python 用 locale 编码（Windows 下为 gbk），
    print 中文/emoji/符号（如 ↳）会抛 UnicodeEncodeError —— MOA 曾因此
    在重定向场景整轮崩溃。调用后编码固定 utf-8 + errors=replace，永不崩。
    """
    for _stream in (_sys.stdout, _sys.stderr):
        try:
            if hasattr(_stream, "reconfigure"):
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 —— 只读流等场景忽略
            pass
