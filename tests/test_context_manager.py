"""
P1 上下文管理器测试

验证：
1. estimate_tokens 轻量估算
2. 未超阈值不压缩
3. 超阈值触发压缩，保留首条 user prompt 与最近 keep_recent 个完整轮次
4. 轮次感知：不会把半截 tool_call 留下（压缩边界落在轮次起点）
5. 不足 keep_recent+1 个轮次时不压缩
"""

import pytest


class _StubLLM:
    """压缩用的 stub：返回固定摘要，记录被调用次数。"""
    def __init__(self):
        self.calls = 0
        self.last_prompt = None

    def invoke(self, prompt):
        self.calls += 1
        self.last_prompt = prompt
        return "摘要内容"


def _build_rounds(n: int, per_round_chars: int = 200) -> list[dict]:
    """构造 n 个完整工具轮次：每个 = 1 条 assistant(tool_calls) + 1 条 tool 结果。"""
    msgs = [{"role": "user", "content": "FINAL_PROMPT"}]
    for i in range(n):
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call_{i}", "name": "bash", "args": {}}],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "name": "bash",
            "content": f"output round {i} " + "x" * (per_round_chars - 14),
        })
    return msgs


class TestEstimateTokens:
    def test_counts_chars(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager()
        cm.messages = [{"role": "user", "content": "abcd" * 100}]
        # 400 字符 // 4 = 100
        assert cm.estimate_tokens() == 100

    def test_tool_calls_counted(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager()
        cm.messages = [{"role": "assistant", "content": "ab",
                       "tool_calls": [{"id": "1", "name": "x", "args": {"k": "value"}}]}]
        # 2 字符 + len(str(tool_call))//4 估算，至少 > 0
        assert cm.estimate_tokens() > 0


class TestMaybeCompress:
    def test_no_compress_under_threshold(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=10000, keep_recent=4)
        cm.messages = _build_rounds(2, per_round_chars=200)  # 远小于预算
        llm = _StubLLM()
        assert cm.maybe_compress(llm) is False
        assert llm.calls == 0

    def test_compress_over_threshold(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=300, keep_recent=2)
        cm.messages = _build_rounds(6, per_round_chars=300)  # 超额
        llm = _StubLLM()
        compressed = cm.maybe_compress(llm)
        assert compressed is True
        # 首条 user prompt 保留
        assert cm.messages[0]["role"] == "user"
        assert cm.messages[0]["content"] == "FINAL_PROMPT"
        # 出现压缩摘要消息
        assert any("<condensed_history>" in m.get("content", "") for m in cm.messages)
        # llm 被调用生成摘要
        assert llm.calls == 1

    def test_keep_recent_rounds_intact(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=300, keep_recent=2)
        cm.messages = _build_rounds(6, per_round_chars=300)
        before_tail = cm.messages[-4:]  # 最后 2 个轮次（4 条）
        cm.maybe_compress(_StubLLM())
        after_tail = cm.messages[-4:]
        # 尾部最近 2 个轮次原样保留
        assert after_tail == before_tail

    def test_round_aware_boundary(self):
        """压缩边界必须落在 'assistant 且带 tool_calls' 起点，不得残留孤儿 tool 消息。"""
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=300, keep_recent=2)
        cm.messages = _build_rounds(6, per_round_chars=300)
        cm.maybe_compress(_StubLLM())
        # 压缩后，任何 tool 消息都应有同 id 对应的 assistant tool_calls 在前
        tool_ids = [m["tool_call_id"] for m in cm.messages if m.get("role") == "tool"]
        for tid in tool_ids:
            found = any(
                m.get("role") == "assistant" and any(
                    tc.get("id") == tid for tc in m.get("tool_calls", [])
                )
                for m in cm.messages
            )
            assert found, f"孤儿 tool 消息: {tid}"

    def test_no_compress_when_too_few_rounds(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=1000, keep_recent=4)
        # 只有 2 个轮次，小于 keep_recent+1=5，不压缩
        cm.messages = _build_rounds(2, per_round_chars=300)
        llm = _StubLLM()
        assert cm.maybe_compress(llm) is False

    def test_no_llm_invoke_returns_false(self):
        from skill_engine.execution.context_manager import ContextManager
        cm = ContextManager(budget=300, keep_recent=2)
        cm.messages = _build_rounds(6, per_round_chars=300)
        # 无 invoke 的 llm → 不压缩，避免崩溃
        assert cm.maybe_compress(object()) is False
