"""记忆写入与调试输出的脱敏工具。

原则：
- 记忆库不保存密钥；写入前命中常见密钥形态直接拒绝。
- ``<memory_context>`` 是运行时注入块，不进入 session JSONL / ctx.json / 导出。
- 日志脱敏必须同时处理嵌套 payload（DebugTracer.event 的 kwargs）。
"""

from __future__ import annotations

import copy
import re
from typing import Any

MEMORY_CONTEXT_RE = re.compile(r"<memory_context\b[^>]*>.*?</memory_context>", re.DOTALL)
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\btvly-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bcfut_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{8,}"
    ),
]


def contains_memory_context(text: Any) -> bool:
    return isinstance(text, str) and bool(MEMORY_CONTEXT_RE.search(text))


def strip_memory_context(text: str) -> str:
    if not isinstance(text, str):
        return text
    return MEMORY_CONTEXT_RE.sub("", text).strip()


def contains_secret(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return any(p.search(text) for p in SECRET_PATTERNS)


def redact_secrets(text: str) -> str:
    if not isinstance(text, str):
        return text
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


_L3_MARKERS = (
    '"sensitivity": "L3"',
    '"sensitivity":"L3"',
    '"sensitivity": "l3"',
    '"sensitivity":"l3"',
)


def redact_for_trace(text: str) -> str:
    """调试日志用：删运行时记忆块、L3 记忆内容，再脱敏密钥。"""
    if isinstance(text, str) and any(m in text for m in _L3_MARKERS):
        return "[L3 memory omitted from trace]"
    return redact_secrets(strip_memory_context(text))


def redact_value(value: Any) -> Any:
    """递归脱敏 DebugTracer payload。"""
    if isinstance(value, str):
        return redact_for_trace(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    return value


def strip_memory_from_messages(messages: list[dict]) -> list[dict]:
    """返回剥离了 memory_context 的消息副本，不修改调用方列表。

    仅含记忆块的 user 消息会被丢弃；含真实用户指令的消息保留剩余文本。
    """
    out: list[dict] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if not contains_memory_context(content):
                out.append(msg)
                continue
            cleaned = strip_memory_context(content)
            if cleaned:
                copy_msg = dict(msg)
                copy_msg["content"] = cleaned
                out.append(copy_msg)
            continue
        if isinstance(content, list):
            had_memory = False
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    if contains_memory_context(part["text"]):
                        had_memory = True
                        text = strip_memory_context(part["text"])
                        if text:
                            parts.append({**part, "text": text})
                    else:
                        parts.append(part)
                else:
                    parts.append(part)
            if had_memory and not parts:
                continue
            if had_memory:
                copy_msg = dict(msg)
                copy_msg["content"] = parts
                out.append(copy_msg)
            else:
                out.append(msg)
            continue
        out.append(msg)
    return out


def redact_messages_for_trace(messages: list[dict]) -> list[dict]:
    """调试落盘用：对消息文本做 memory_context / L3 / 密钥脱敏。"""
    out: list[dict] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, str):
            new = redact_for_trace(content)
            out.append({**msg, "content": new} if new != content else msg)
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append({**part, "text": redact_for_trace(part["text"])})
                else:
                    parts.append(part)
            out.append({**msg, "content": parts})
        else:
            out.append(msg)
    return out


def strip_memory_from_state(state: dict) -> dict:
    """复制 state 并剥离 messages 中的运行时记忆块。"""
    if not isinstance(state, dict):
        return state
    out = copy.deepcopy(state)
    if isinstance(out.get("messages"), list):
        out["messages"] = strip_memory_from_messages(out["messages"])
    return out
