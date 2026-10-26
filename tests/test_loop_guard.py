"""loop_guard 参数调用指纹环检测 —— 纯逻辑测试（零 LLM、零执行器）。

覆盖：
- 指纹稳定性 / 键序无关 / 空白归一化 / 工具名区分 / 不可序列化兜底
- LoopGuard 状态机：连续计数、缺席清零、软警告一次性、只读放宽、阈值覆盖
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skill_engine.execution.tool_exec.loop_guard import (
    _DEFAULT_STOP_LIMIT,
    _READONLY_STOP_LIMIT,
    LoopGuard,
    loop_feedback_message,
    loop_stop_message,
    tool_call_fingerprint,
)


def _fp(name="bash", args=None):
    return tool_call_fingerprint(name, args or {"command": "ls"})


class TestFingerprint:
    def test_stable_for_identical_calls(self):
        assert _fp() == _fp()

    def test_differs_by_args(self):
        assert _fp(args={"command": "ls"}) != _fp(args={"command": "ls -la"})

    def test_differs_by_tool_name(self):
        assert _fp("bash") != _fp("read_file")

    def test_key_order_irrelevant(self):
        a = tool_call_fingerprint("edit", {"path": "x.py", "old": "a"})
        b = tool_call_fingerprint("edit", {"old": "a", "path": "x.py"})
        assert a == b

    def test_whitespace_normalized_in_string_values(self):
        a = tool_call_fingerprint("bash", {"command": "ls  -la\nfoo"})
        b = tool_call_fingerprint("bash", {"command": "ls -la foo"})
        assert a == b

    def test_nested_dict_whitespace_normalized(self):
        a = tool_call_fingerprint("t", {"outer": {"inner": "a  b"}})
        b = tool_call_fingerprint("t", {"outer": {"inner": "a b"}})
        assert a == b

    def test_non_string_types_not_collapsed(self):
        # 数值参数不参与空白折叠（也没有空白），且 int/float 不混淆
        assert tool_call_fingerprint("t", {"n": 1}) != tool_call_fingerprint("t", {"n": 2})

    def test_unserializable_args_does_not_raise(self):
        class Weird:
            def __repr__(self):
                return "<weird>"
        fp = tool_call_fingerprint("t", {"obj": Weird()})
        assert isinstance(fp, str) and len(fp) == 40

    def test_none_args_fallback(self):
        fp = tool_call_fingerprint("t", None)
        assert isinstance(fp, str) and len(fp) == 40


class TestLoopGuardStateMachine:
    def test_first_occurrence_ok(self):
        g = LoopGuard(enabled=True)
        assert g.feed_round([(_fp(), "bash")]) == ("ok", None)

    def test_write_tool_warn_then_stop(self):
        g = LoopGuard(enabled=True)
        fp = _fp()
        assert g.feed_round([(fp, "bash")])[0] == "ok"        # streak 1
        action, info = g.feed_round([(fp, "bash")])           # streak 2 = warn
        assert action == "warn"
        assert info["tool"] == "bash" and info["streak"] == 2
        assert g.feed_round([(fp, "bash")])[0] == "stop"      # streak 3 = stop

    def test_readonly_tool_relaxed_limit(self):
        g = LoopGuard(enabled=True)
        fp = _fp("read_file", {"path": "a.py"})
        for _ in range(_READONLY_STOP_LIMIT - 1):
            assert g.feed_round([(fp, "read_file")])[0] != "stop"
        assert g.feed_round([(fp, "read_file")])[0] == "stop"

    def test_absent_round_resets_streak(self):
        g = LoopGuard(enabled=True)
        fp = _fp()
        g.feed_round([(fp, "bash")])
        g.feed_round([(fp, "bash")])                          # streak 2 → warn
        g.feed_round([])                                      # 文本轮 → 清零
        action, _ = g.feed_round([(fp, "bash")])
        assert action == "ok"                                 # streak 重新从 1 起

    def test_changed_args_resets_streak(self):
        g = LoopGuard(enabled=True)
        g.feed_round([(_fp(args={"command": "ls"}), "bash")])
        g.feed_round([(_fp(args={"command": "ls"}), "bash")])  # warn
        action, _ = g.feed_round([(_fp(args={"command": "ls -la"}), "bash")])
        assert action == "ok"                                  # 参数变了 → 新指纹

    def test_warn_fires_only_once_per_fingerprint(self):
        g = LoopGuard(enabled=True)
        fp = _fp()
        g.feed_round([(fp, "bash")])
        assert g.feed_round([(fp, "bash")])[0] == "warn"
        # streak 不变时重复 feed 不会再 warn（信息不重复刷屏）
        assert g.feed_round([(fp, "bash")])[0] == "stop"

    def test_limit_override(self):
        g = LoopGuard(enabled=True, limit_override=5)
        fp = _fp()
        for _ in range(4):
            g.feed_round([(fp, "bash")])
        assert g.feed_round([(fp, "bash")])[0] == "stop"

    def test_disabled_guard_always_ok(self):
        g = LoopGuard(enabled=False)
        fp = _fp()
        for _ in range(10):
            assert g.feed_round([(fp, "bash")]) == ("ok", None)

    def test_multiple_tool_calls_same_round_counted(self):
        g = LoopGuard(enabled=True)
        fp1 = _fp("bash", {"command": "a"})
        fp2 = _fp("bash", {"command": "b"})
        g.feed_round([(fp1, "bash"), (fp2, "bash")])
        action, info = g.feed_round([(fp1, "bash"), (fp2, "bash")])
        assert action == "warn"
        assert info["fingerprint"] in (fp1, fp2)

    def test_worst_streak_wins_when_both_exceed(self):
        g = LoopGuard(enabled=True, limit_override=3)
        fp_old = _fp("bash", {"command": "a"})
        fp_new = _fp("bash", {"command": "b"})
        g.feed_round([(fp_old, "bash")])
        g.feed_round([(fp_old, "bash")])
        # 第 3 轮同时出现旧指纹（streak 3 → stop）和新指纹（streak 1）
        action, info = g.feed_round([(fp_old, "bash"), (fp_new, "bash")])
        assert action == "stop"
        assert info["fingerprint"] == fp_old


class TestDefaults:
    def test_default_stop_limit_is_strict_for_write_tools(self):
        assert _DEFAULT_STOP_LIMIT == 3

    def test_readonly_limit_relaxed(self):
        assert _READONLY_STOP_LIMIT > _DEFAULT_STOP_LIMIT

    def test_env_toggle_off(self, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_LOOP_GUARD", "off")
        assert LoopGuard().enabled is False

    def test_env_limit_override(self, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_LOOP_GUARD_LIMIT", "7")
        assert LoopGuard().limit_override == 7

    def test_env_invalid_limit_falls_back(self, monkeypatch):
        monkeypatch.setenv("SKILLS_ENGINE_LOOP_GUARD_LIMIT", "abc")
        assert LoopGuard().limit_override is None


class TestMessages:
    def test_feedback_message_mentions_tool_and_streak(self):
        msg = loop_feedback_message("bash", 2)
        assert "bash" in msg and "2" in msg

    def test_stop_message_mentions_loop(self):
        msg = loop_stop_message("edit_file", 3)
        assert "edit_file" in msg and "死循环" in msg
