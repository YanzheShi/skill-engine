"""CliHumanIO.read() 单测：大段粘贴外置成 token，小输入原样返回。

不依赖真实 TTY：用 fake session 替掉 _get_session，直接验证外置逻辑。
"""

import re

from skill_engine.execution.human_io import CliHumanIO


def _fake_session(returning):
    return type("FakeSession", (), {"prompt": lambda self, p: returning})()


def test_small_input_passthrough(monkeypatch):
    hio = CliHumanIO()
    monkeypatch.setattr(hio, "_get_session", lambda: _fake_session("hi there"))
    assert hio.read(prompt="> ") == "hi there"


def test_large_paste_externalized_to_token(monkeypatch, tmp_path):
    big = "\n".join(f"log line {i}" for i in range(10))  # 超阈值
    hio = CliHumanIO(paste_dir=tmp_path)
    monkeypatch.setattr(hio, "_get_session", lambda: _fake_session(big))
    out = hio.read(prompt="> ")
    assert out is not None
    m = re.match(r"\[Pasted text #(\d+): (\d+) lines → (.+?)\]", out)
    assert m, f"大段粘贴应返回引用 token，实际: {out!r}"
    assert int(m.group(2)) == 10
    # 落盘文件确实存在且内容一致
    path = m.group(3)
    saved = (tmp_path / path.split("/")[-1]).read_text(encoding="utf-8")
    assert saved == big


def test_get_session_never_raises(monkeypatch):
    # 无真实控制台（headless/管道）时优雅回退为 None，绝不抛异常；
    # 真实 Windows 控制台下会构造出 PromptSession。两种情况都合法。
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    hio = CliHumanIO()
    sess = hio._get_session()
    assert sess is None or type(sess).__name__ == "PromptSession"


def test_load_file_as_paste(tmp_path):
    from skill_engine.execution.runner import _load_file_as_paste
    f = tmp_path / "log.txt"
    content = "\n".join(f"line {i}" for i in range(10))
    f.write_text(content, encoding="utf-8")
    out = _load_file_as_paste(str(f), tmp_path / "pastes")
    m = re.match(r"\[Pasted text #(\d+): (\d+) lines → (.+?)\]", out)
    assert m, f":load 应返回引用 token，实际: {out!r}"
    assert int(m.group(2)) == 10
    saved = (tmp_path / "pastes" / m.group(3).split("/")[-1]).read_text(encoding="utf-8")
    assert saved == content


def test_load_file_missing(tmp_path):
    from skill_engine.execution.runner import _load_file_as_paste
    out = _load_file_as_paste(str(tmp_path / "nope.txt"), tmp_path / "pastes")
    assert out.startswith("[session] :load 失败")

