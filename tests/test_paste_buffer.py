"""粘贴外置（Hermes-lite）单元测试：落盘 / 阈值 / token 解析。

不依赖 prompt_toolkit：human_io 的 PT 接入由 guarded import 在非 TTY 下
自动回退，这里只测纯逻辑层 paste_buffer。
"""

import re

import pytest

from skill_engine.execution import paste_buffer as pb


def test_save_paste_below_threshold_returns_none():
    assert pb.save_paste("hello world") is None
    assert pb.save_paste("a\nb") is None  # 2 行 + 短，不触发


def test_save_paste_writes_file_and_returns_token(tmp_path):
    content = "\n".join(f"log line {i}" for i in range(10))  # 10 行，超阈值
    token = pb.save_paste(content, base=tmp_path)
    assert token is not None
    m = re.match(r"\[Pasted text #(\d+): (\d+) lines → (.+?)\]", token)
    assert m, f"token 格式不符: {token}"
    assert int(m.group(2)) == 10
    path = m.group(3)
    saved = (tmp_path / path.split("/")[-1]).read_text(encoding="utf-8")
    assert saved == content


def test_resolve_refs_replaces_token_and_lists_paths():
    text = "看这个 [Pasted text #1: 8 lines → D:\\tmp\\paste_1_123456.txt] 结束"
    cleaned, paths = pb.resolve_refs(text)
    assert len(paths) == 1
    assert "D:\\tmp\\paste_1_123456.txt" in str(paths[0])
    assert "已保存至" in cleaned
    assert "[Pasted text" not in cleaned


def test_resolve_refs_no_token_passthrough():
    assert pb.resolve_refs("普通指令") == ("普通指令", [])


def test_save_paste_carriage_return_split(tmp_path):
    content = "a\r\nb\r\nc\r\nd\r\ne"  # 5 行（含 \r\n）
    token = pb.save_paste(content, base=tmp_path)
    m = re.match(r"\[Pasted text #(\d+): (\d+) lines → (.+?)\]", token)
    assert int(m.group(2)) == 5
