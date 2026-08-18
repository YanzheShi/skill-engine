"""
上下文三级渐进压缩测试（P0 S0-3）

覆盖：
- token 估算中文修正（ASCII 1/4、非 ASCII 1）与纯 ASCII 向后兼容
- L1 旧工具输出折叠（阈值 / 幂等 / opt-out / 不动最近轮次）
- L2 结构化压缩默认中立模板 + compress_template 覆盖
- L3 摘要失败时的截断兜底
- default_context_budget 环境变量覆盖
"""

import pytest


class _StubLLM:
    def __init__(self, reply="摘要内容"):
        self.calls = 0
        self.last_prompt = None
        self.reply = reply

    def invoke(self, prompt):
        self.calls += 1
        self.last_prompt = prompt
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _build_rounds(n, tool_chars=200):
    msgs = [{"role": "user", "content": "FINAL_PROMPT"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"call_{i}", "name": "bash", "args": {}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}", "name": "bash",
                     "content": f"output round {i} " + "x" * max(0, tool_chars - 14)})
    return msgs


class TestEstimateTokens:
    def test_ascii_backward_compatible(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager()
        cm.messages = [{"role": "user", "content": "abcd" * 100}]
        assert cm.estimate_tokens() == 100  # 400 ASCII // 4

    def test_chinese_not_underestimated(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager()
        cm.messages = [{"role": "user", "content": "中文" * 100}]  # 200 非 ASCII 字符
        assert cm.estimate_tokens() == 200  # 旧实现会估成 50


class TestL1MicroCompact:
    def test_folds_old_large_tool_output(self):
        from skill_engine.execution.context_manager import ContextManager, FOLD_THRESHOLD
        cm = ContextManager(budget=10 ** 9, keep_recent=2)  # 预算巨大 → 不会触发 L2
        cm.messages = _build_rounds(6, tool_chars=FOLD_THRESHOLD + 1000)
        llm = _StubLLM()
        changed = cm.maybe_compress(llm)
        assert changed is True
        assert llm.calls == 0  # 纯 L1，未动用 LLM
        # 旧轮次被折叠
        old_tools = [m for m in cm.messages if m.get("role") == "tool"][:4]
        assert all(m["content"].startswith("[已折叠") for m in old_tools)
        # 最近 2 轮原样保留
        recent = [m for m in cm.messages if m.get("role") == "tool"][-2:]
        assert all(not m["content"].startswith("[已折叠") for m in recent)

    def test_idempotent(self):
        from skill_engine.execution.context_manager import ContextManager, FOLD_THRESHOLD
        cm = ContextManager(budget=10 ** 9, keep_recent=2)
        cm.messages = _build_rounds(6, tool_chars=FOLD_THRESHOLD + 1000)
        assert cm.maybe_compress(_StubLLM()) is True
        assert cm.maybe_compress(_StubLLM()) is False  # 已折叠的不再处理

    def test_opt_out(self):
        from skill_engine.execution.context_manager import ContextManager, FOLD_THRESHOLD
        cm = ContextManager(budget=10 ** 9, keep_recent=2, compact_tool_output=False)
        cm.messages = _build_rounds(6, tool_chars=FOLD_THRESHOLD + 1000)
        assert cm.maybe_compress(_StubLLM()) is False
        assert all(not m["content"].startswith("[已折叠")
                   for m in cm.messages if m.get("role") == "tool")


class TestL2StructuredSummary:
    def test_default_neutral_schema(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=300, keep_recent=2)
        cm.messages = _build_rounds(6, tool_chars=300)
        llm = _StubLLM()
        assert cm.maybe_compress(llm) is True
        assert llm.calls == 1
        # 默认模板是任务中立 schema（不含 coding 专属字段）
        assert "原始请求" in llm.last_prompt
        assert "关键对象引用" in llm.last_prompt
        assert any("<condensed_history>" in m.get("content", "") for m in cm.messages)

    def test_custom_template_override(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=300, keep_recent=2,
                            summary_prompt="CUSTOM-TEMPLATE\n")
        cm.messages = _build_rounds(6, tool_chars=300)
        llm = _StubLLM()
        cm.maybe_compress(llm)
        assert llm.last_prompt.startswith("CUSTOM-TEMPLATE")


class TestL2ClipToBudget:
    def test_summary_prompt_clipped_when_segment_huge(self):
        """摘要 prompt 不得无限膨胀：超长历史先裁到预算内，否则调用必然超窗口失败。"""
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=500, keep_recent=2, compact_tool_output=False)
        cm.messages = _build_rounds(6, tool_chars=4000)  # 4 条旧 tool 输出各 1000 token
        llm = _StubLLM()
        assert cm.maybe_compress(llm) is True
        assert llm.calls == 1
        # cap = max(500*0.8*0.5, 2000) = 2000 token ≈ 8000 ASCII 字符；原始历史约 16000
        assert len(llm.last_prompt) < 9000
        assert "[历史过长" in llm.last_prompt

    def test_small_segment_not_clipped(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=500, keep_recent=2, compact_tool_output=False)
        cm.messages = _build_rounds(6, tool_chars=1800)  # 4 旧轮 1800 token < cap 2000
        llm = _StubLLM()
        assert cm.maybe_compress(llm) is True
        assert llm.calls == 1
        assert "[历史过长" not in llm.last_prompt

    def test_summary_error_detail_recorded(self):
        """失败详情必须被记录，供 UI 透出（此前仅日志前缀，看不到具体原因）。"""
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=300, keep_recent=2)
        cm.messages = _build_rounds(6, tool_chars=300)
        llm = _StubLLM(reply=RuntimeError("boom"))
        assert cm.maybe_compress(llm) is True
        assert "boom" in str(cm._last_summary_error)


class TestL3Truncation:
    def test_summary_failure_falls_back_to_truncation(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=300, keep_recent=2)
        cm.messages = _build_rounds(6, tool_chars=300)
        before_tail = cm.messages[-4:]
        llm = _StubLLM(reply=RuntimeError("boom"))
        assert cm.maybe_compress(llm) is True
        # 首条保留、显式告知插入、最近轮次保留、无 condensed_history
        assert cm.messages[0]["content"] == "FINAL_PROMPT"
        assert any("[系统提示]" in m.get("content", "") for m in cm.messages)
        assert not any("<condensed_history>" in m.get("content", "") for m in cm.messages)
        assert cm.messages[-4:] == before_tail


class TestL2RateLimitRetry:
    """429 限流（rpm exhausted）退避重试：等 RPM 窗口过去再试，不急着截断。"""

    def _cm(self, monkeypatch, delays=(0.01, 0.01)):
        import skill_engine.execution.context_manager as cm_mod
        monkeypatch.setattr(cm_mod, "RPM_RETRY_DELAYS", delays)
        monkeypatch.setattr(cm_mod.time, "sleep", lambda s: None)  # 测试不真等
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=300, keep_recent=2)
        cm.messages = _build_rounds(6, tool_chars=300)
        return cm

    def test_429_then_success_returns_summary(self, monkeypatch):
        """首次 429 → 等待 → 重试成功：必须产出摘要，不得走截断。"""
        cm = self._cm(monkeypatch)

        class _FlakyLLM:
            def __init__(self):
                self.calls = 0

            def invoke(self, prompt):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("Error code: 429 - rpm exhausted, code '8'")
                return "重试成功的摘要"

        llm = _FlakyLLM()
        assert cm.maybe_compress(llm) is True
        assert llm.calls == 2
        assert any("<condensed_history>" in m.get("content", "") for m in cm.messages)
        assert not any("[系统提示]" in m.get("content", "") for m in cm.messages)

    def test_429_always_fails_after_retries_then_truncate(self, monkeypatch):
        """持续 429：按退避表重试后仍失败，才走截断兜底。"""
        cm = self._cm(monkeypatch)

        class _Always429:
            def __init__(self):
                self.calls = 0

            def invoke(self, prompt):
                self.calls += 1
                raise RuntimeError("Error code: 429 - {'message': 'rpm exhausted'}")

        llm = _Always429()
        assert cm.maybe_compress(llm) is True
        assert llm.calls == 3  # 初次 + 2 次退避重试
        assert any("[系统提示]" in m.get("content", "") for m in cm.messages)
        assert "429" in str(cm._last_summary_error)

    def test_non_429_error_no_retry(self, monkeypatch):
        """非限流错误不重试（避免 5xx/参数错误反复空等）。"""
        cm = self._cm(monkeypatch)
        llm = _StubLLM(reply=RuntimeError("boom"))
        assert cm.maybe_compress(llm) is True
        assert llm.calls == 1
        assert any("[系统提示]" in m.get("content", "") for m in cm.messages)


class TestDefaultBudget:
    def test_env_override(self, monkeypatch):
        from skill_engine.execution.context_manager import default_context_budget
        monkeypatch.setenv("SKILLS_ENGINE_CONTEXT_BUDGET", "4096")
        assert default_context_budget() == 4096

    def test_default_value(self, monkeypatch):
        from skill_engine.execution.context_manager import (
            default_context_budget, DEFAULT_CONTEXT_BUDGET)
        monkeypatch.delenv("SKILLS_ENGINE_CONTEXT_BUDGET", raising=False)
        assert default_context_budget() == DEFAULT_CONTEXT_BUDGET

    def test_invalid_env_falls_back(self, monkeypatch):
        from skill_engine.execution.context_manager import (
            default_context_budget, DEFAULT_CONTEXT_BUDGET)
        monkeypatch.setenv("SKILLS_ENGINE_CONTEXT_BUDGET", "not-a-number")
        assert default_context_budget() == DEFAULT_CONTEXT_BUDGET
