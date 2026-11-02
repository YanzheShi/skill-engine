"""执行效率 P0 修复的回归测试。

覆盖：
- write_file 的 append 模式（断点续写）与完整度回执
- 缺 path 时给出「输出被截断」的语义化提示，而不是让模型补路径后重试
- edit_file 缺 path 不再误报「目标路径是目录」
- max_tokens 默认注入（可被 env / profile 覆盖，0 = 不下发）
- 沙箱预热点火在关闭时不启动线程
"""

from types import SimpleNamespace

from skill_engine.config import (
    DEFAULT_LLM_MAX_TOKENS,
    _apply_max_tokens,
    _apply_temperature,
    llm_max_tokens,
    llm_temperature,
)
from skill_engine.execution.context_manager import TC_ARGS_FOLD_THRESHOLD, ContextManager
from skill_engine.execution.executor import Executor
from skill_engine.execution.tool_exec import guard as truncation_guard
from skill_engine.execution.tool_exec.handlers.edit_file import EditFileHandler
from skill_engine.execution.tool_exec.handlers.write_file import WriteFileHandler


def _ctx(tmp_path, confirm_mode=""):
    """write_file 只 duck-type 这几个属性，用 SimpleNamespace 足够。"""
    return SimpleNamespace(
        base_dir=tmp_path,
        skill=None,
        confirm_edits_mode=confirm_mode,
        snapshot=SimpleNamespace(record=lambda *a, **k: None),
        file_tracker=SimpleNamespace(
            on_write=lambda *a, **k: None,
            check_editable=lambda *a, **k: (True, ""),
        ),
        check_file_safety=lambda *a, **k: (True, ""),
        confirm_edit=lambda *a, **k: True,
    )


class TestWriteFileAppend:
    def test_append_continues_existing_file(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("line1\nline2", encoding="utf-8")   # 故意不带结尾换行
        res = WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "a.txt", "content": "line3\n", "mode": "append"}},
            _ctx(tmp_path))
        assert f.read_text(encoding="utf-8") == "line1\nline2\nline3\n"
        assert "mode=append" in res.content
        # 回执是摘要（字节/行数/尾片段），不能把整份内容再吐回上下文
        assert len(res.content) < 400

    def test_append_to_missing_file_degrades_to_create(self, tmp_path):
        res = WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "new.txt", "content": "hi\n", "mode": "append"}},
            _ctx(tmp_path))
        assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hi\n"
        assert "mode=overwrite" in res.content

    def test_overwrite_default_unchanged(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("old\n", encoding="utf-8")
        WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "a.txt", "content": "new\n"}},
            _ctx(tmp_path))
        assert f.read_text(encoding="utf-8") == "new\n"

    def test_invalid_mode_rejected(self, tmp_path):
        res = WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "a.txt", "content": "x", "mode": "merge"}},
            _ctx(tmp_path))
        assert "mode 只支持" in res.content
        assert not (tmp_path / "a.txt").exists()


class TestCompletenessReceipt:
    """回执要让模型不必再写 python -c 自查（实测这样自查了 6 次 × 一整轮）。"""

    def test_html_incomplete_flagged(self, tmp_path):
        res = WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "i.html", "content": "<html><body><script>var a=1"}},
            _ctx(tmp_path))
        assert "complete: NO" in res.content
        assert 'mode="append"' in res.content

    def test_html_complete_flagged(self, tmp_path):
        res = WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "i.html", "content": "<html><body>ok</body></html>\n"}},
            _ctx(tmp_path))
        assert "complete: yes" in res.content

    def test_receipt_reports_size_and_tail(self, tmp_path):
        res = WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "a.txt", "content": "alpha\nbeta\n"}},
            _ctx(tmp_path))
        assert "2 lines" in res.content
        assert "tail:" in res.content

    def test_json_completeness(self, tmp_path):
        bad = WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "a.json", "content": '{"a": 1'}},
            _ctx(tmp_path))
        assert "complete: NO" in bad.content
        good = WriteFileHandler().execute(
            {"id": "c2", "type": "write_file",
             "input": {"path": "b.json", "content": '{"a": 1}'}},
            _ctx(tmp_path))
        assert "complete: yes" in good.content

    def test_verdict_reaches_trace_channel(self, tmp_path):
        """完整度判定必须走 emit_result —— 只有那条通道会落 trace 事件。

        第一次 A/B 跑完才发现日志里搜不到 complete:，因为 print_line 只是裸 print。
        """
        seen = []
        ctx = _ctx(tmp_path)
        ctx.emit_result = lambda out: seen.append(out)
        WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "i.html", "content": "<html><body>cut here"}},
            ctx)
        assert len(seen) == 1
        assert "complete: NO" in seen[0]


class TestTruncatedCallHint:
    def test_write_file_missing_path_mentions_truncation(self, tmp_path):
        res = WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"content": "<html>" * 100}},
            _ctx(tmp_path))
        assert "max_tokens" in res.content
        assert 'mode="append"' in res.content
        # 旧文案把缺 path 当成模型笔误，会诱导它原样重发整文件
        assert "请补全" not in res.content

    def test_edit_file_missing_path_not_directory_error(self):
        res = EditFileHandler().execute(
            {"id": "c1", "type": "edit_file",
             "input": {"edits": [{"oldText": "a", "newText": "b"}]}},
            SimpleNamespace(base_dir=None))
        assert "缺少必填参数" in res.content
        assert "目录" not in res.content


class TestMaxTokensDefault:
    def test_injected_when_absent(self, monkeypatch):
        monkeypatch.delenv("SKILLS_ENGINE_LLM_MAX_TOKENS", raising=False)
        cfg = {}
        _apply_max_tokens(cfg)
        assert cfg["max_tokens"] == DEFAULT_LLM_MAX_TOKENS == 8192

    def test_profile_value_wins(self, monkeypatch):
        monkeypatch.delenv("SKILLS_ENGINE_LLM_MAX_TOKENS", raising=False)
        cfg = {"max_tokens": 1234}
        _apply_max_tokens(cfg)
        assert cfg["max_tokens"] == 1234

    def test_env_override_and_zero_disables(self, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_LLM_MAX_TOKENS", "4096")
        cfg = {}
        _apply_max_tokens(cfg)
        assert cfg["max_tokens"] == 4096

        monkeypatch.setenv("SKILLS_ENGINE_LLM_MAX_TOKENS", "0")
        cfg2 = {}
        _apply_max_tokens(cfg2)
        assert "max_tokens" not in cfg2
        assert llm_max_tokens() == 0


class TestTemperatureControl:
    """采样温度可固定 —— A/B 基准的前提。此前 models: 段写 temperature 会被静默丢弃。"""

    def test_default_is_not_sent(self, monkeypatch):
        monkeypatch.delenv("SKILLS_ENGINE_LLM_TEMPERATURE", raising=False)
        assert llm_temperature() is None
        cfg = {}
        _apply_temperature(cfg)
        assert "temperature" not in cfg

    def test_zero_is_injected(self, monkeypatch):
        # 0 是合法值，不能被 falsy 判断吞掉
        monkeypatch.setenv("SKILLS_ENGINE_LLM_TEMPERATURE", "0")
        assert llm_temperature() == 0.0
        cfg = {}
        _apply_temperature(cfg)
        assert cfg["temperature"] == 0.0

    def test_profile_value_wins(self, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_LLM_TEMPERATURE", "0")
        cfg = {"temperature": 0.7}
        _apply_temperature(cfg)
        assert cfg["temperature"] == 0.7

    def test_invalid_env_degrades_to_none(self, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_LLM_TEMPERATURE", "abc")
        assert llm_temperature() is None

    def test_models_section_passes_sampling_keys(self, monkeypatch, tmp_path):
        """models: 段的 temperature/seed/max_tokens 必须透传，不能只留 5 个键。"""
        from skill_engine.config import _parse_model_entries
        entries = _parse_model_entries([
            {"name": "p", "model": "m", "api_key": "k", "base_url": "u",
             "temperature": 0, "seed": 42, "max_tokens": 8192},
        ])
        assert entries["p"]["temperature"] == 0
        assert entries["p"]["seed"] == 42
        assert entries["p"]["max_tokens"] == 8192


class TestSandboxPrewarm:
    def test_no_thread_when_sandbox_disabled(self, tmp_path):
        ex = Executor(timeout=1, sandbox=False)
        assert ex.prewarm_sandbox(tmp_path) is False

    def test_env_gate_disables_prewarm(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_SANDBOX_PREWARM", "off")
        ex = Executor(timeout=1, sandbox=True)
        assert ex.prewarm_sandbox(tmp_path) is False

    def test_idempotent(self, tmp_path, monkeypatch):
        # 第二次调用不应再起线程（用 env 关掉真实的 srt 准备，只验证状态机）
        monkeypatch.setenv("SKILLS_ENGINE_SANDBOX_PREWARM", "off")
        ex = Executor(timeout=1, sandbox=True)
        assert ex.prewarm_sandbox(tmp_path) is False
        assert ex.prewarm_sandbox(tmp_path) is False


class TestAtomicStaging:
    """暂存：不完整写不转正；补齐后原子替换。"""

    def test_incomplete_html_stays_partial(self, tmp_path):
        res = WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "i.html", "content": "<html><body>x"}},
            _ctx(tmp_path))
        assert not (tmp_path / "i.html").exists()
        assert (tmp_path / "i.html.partial").exists()
        assert "complete: NO" in res.content
        assert "NOT written" in res.content

    def test_append_completes_and_finalizes(self, tmp_path):
        ctx = _ctx(tmp_path)
        WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "i.html", "content": "<html><body>x"}}, ctx)
        res = WriteFileHandler().execute(
            {"id": "c2", "type": "write_file",
             "input": {"path": "i.html", "content": "</body></html>",
                       "mode": "append"}}, ctx)
        assert (tmp_path / "i.html").read_text(encoding="utf-8") == \
            "<html><body>x\n</body></html>"
        assert not (tmp_path / "i.html.partial").exists()
        assert "complete: yes" in res.content

    def test_edit_fills_marker_and_finalizes(self, tmp_path):
        ctx = _ctx(tmp_path)
        WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "i.html",
                       "content": "<html><body><!--@C0@--></body></html>"}}, ctx)
        # 残留 marker → 不转正
        assert not (tmp_path / "i.html").exists()
        assert (tmp_path / "i.html.partial").exists()
        res = EditFileHandler().execute(
            {"id": "c2", "type": "edit_file",
             "input": {"path": "i.html",
                       "edits": [{"oldText": "<!--@C0@-->",
                                  "newText": "<div>ok</div>"}]}}, ctx)
        assert (tmp_path / "i.html").read_text(encoding="utf-8") == \
            "<html><body><div>ok</div></body></html>"
        assert not (tmp_path / "i.html.partial").exists()
        assert "applied 1 edits" in res.content

    def test_atomic_write_can_be_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_ATOMIC_WRITE", "0")
        WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "i.html", "content": "<html><body>x"}},
            _ctx(tmp_path))
        assert (tmp_path / "i.html").exists()          # 旧行为：直接写目标
        assert not (tmp_path / "i.html.partial").exists()


class TestTruncationGuard:
    """守卫只管**参数结构**；内容语义完整性交给暂存层。"""

    def test_short_skeleton_not_refused(self, tmp_path):
        """线上回归：故意写的小骨架/半成品不能被守卫误杀（否则模型空转）。"""
        tc = {"id": "t1", "type": "write_file",
              "input": {"path": "a.html", "content": "A"}}
        assert not truncation_guard.should_refuse(tc, "length")
        res = WriteFileHandler().execute(tc, _ctx(tmp_path))
        assert "complete: NO" in res.content          # 暂存层判定不完整
        assert not (tmp_path / "a.html").exists()      # 但不转正，也不拒执行
        assert (tmp_path / "a.html.partial").exists()

    def test_length_does_not_refuse_structurally_ok(self):
        tc = {"type": "write_file", "input": {"path": "a.txt", "content": "ok"}}
        assert not truncation_guard.should_refuse(tc, "length")
        assert truncation_guard.is_truncated_turn("length")
        assert not truncation_guard.is_truncated_turn("stop")

    def test_missing_content_still_refused(self):
        assert truncation_guard.should_refuse(
            {"type": "write_file", "input": {"path": "a.html"}}, "")
        assert truncation_guard.should_refuse(
            {"type": "write_file", "input": {"path": "a.html", "content": ""}}, "")

    def test_edit_args_incomplete(self):
        assert truncation_guard.should_refuse(
            {"type": "edit_file", "input": {"edits": [{"oldText": "a"}]}}, "")
        assert not truncation_guard.should_refuse(
            {"type": "edit_file",
             "input": {"edits": [{"oldText": "a", "newText": "b"}]}}, "")

    def test_non_dict_args_refused(self):
        # 某些 provider 的 args 是残缺 JSON 字符串，不能直接 .get
        assert truncation_guard.should_refuse(
            {"type": "write_file", "input": '{"path": "a.html", "cont'}, "")

    def test_non_write_tools_not_refused(self):
        assert not truncation_guard.should_refuse(
            {"type": "bash", "input": {"command": "ls"}}, "length")


class TestTruncatedTurnStaging:
    """撞上限的那一轮：内容照写进 partial，但不转正（防半截代码成为正式文件）。"""

    def test_unknown_type_not_finalized_when_truncated(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.turn_truncated = True
        WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "m.py", "content": "def f():\n    return 1"}}, ctx)
        assert not (tmp_path / "m.py").exists()
        assert (tmp_path / "m.py.partial").exists()

    def test_unknown_type_finalized_when_not_truncated(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.turn_truncated = False
        WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "m.py", "content": "def f():\n    return 1"}}, ctx)
        assert (tmp_path / "m.py").read_text(encoding="utf-8") == \
            "def f():\n    return 1"

    def test_closed_html_finalizes_even_if_turn_truncated(self, tmp_path):
        """可证明完整（含 </html>）时，即使本轮撞上限也允许转正。"""
        ctx = _ctx(tmp_path)
        ctx.turn_truncated = True
        WriteFileHandler().execute(
            {"id": "c1", "type": "write_file",
             "input": {"path": "ok.html",
                       "content": "<html><body>hi</body></html>"}}, ctx)
        assert (tmp_path / "ok.html").exists()

    def test_paginated_build_survives_truncated_turns(self, tmp_path):
        """复现线上场景：每一轮都撞 max_tokens，仍靠 partial + append 拼出完整文件。"""
        ctx = _ctx(tmp_path)
        ctx.turn_truncated = True
        chunks = ['<html><head><title>t</title></head><body>\n',
                  '<div>1</div>\n',
                  '<div>2</div></body></html>\n']
        for i, ch in enumerate(chunks):
            WriteFileHandler().execute(
                {"id": f"c{i}", "type": "write_file",
                 "input": {"path": "big.html", "content": ch,
                           "mode": "overwrite" if i == 0 else "append"}}, ctx)
            if i < len(chunks) - 1:
                assert not (tmp_path / "big.html").exists()   # 中途不产出半成品
        assert (tmp_path / "big.html").read_text(encoding="utf-8") == "".join(chunks)
        assert not (tmp_path / "big.html.partial").exists()


class TestToolCallArgsFold:
    """上下文回收：旧轮巨型 args 折叠，最近一轮保持完整。"""

    def test_old_args_folded_recent_kept(self):
        big = "X" * (TC_ARGS_FOLD_THRESHOLD + 10)
        cm = ContextManager(keep_recent=1, compact_tool_output=True)
        cm.messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "1", "name": "write_file",
                             "args": {"path": "a.html", "content": big}}]},
            {"role": "tool", "tool_call_id": "1", "name": "write_file", "content": "ok"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "2", "name": "write_file",
                             "args": {"path": "b.html", "content": big}}]},
            {"role": "tool", "tool_call_id": "2", "name": "write_file", "content": "ok"},
        ]
        assert cm._micro_compact() is True
        old = cm.messages[2]["tool_calls"][0]["args"]["content"]
        recent = cm.messages[5]["tool_calls"][0]["args"]["content"]
        assert old.startswith("[content omitted")
        assert recent == big                       # keep_recent 窗口内不折
