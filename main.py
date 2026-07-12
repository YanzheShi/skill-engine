#!/usr/bin/env python3
"""CLI entry point — 用于 PyCharm 调试

用法：
    python main.py list
    python main.py match "题解"
    python main.py run "部署" --llm
    python main.py info leetcode-solution-writer
"""
from skill_engine.cli import app

if __name__ == "__main__":
    app()