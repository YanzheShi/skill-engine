"""会话蒸馏：规则候选 + 可选 LLM 候选 + episode 摘要。

P2 的设计取舍：
- 规则抽取零 LLM 成本，负责 key_finding / 用户纠正 / 压缩 episode；
- LLM 只做“候选生成”，不直接写库；由 MemoryService 决定自动提交还是进 INBOX；
- 蒸馏失败绝不能影响会话退出。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import CORE_TYPES
from .redact import contains_secret, strip_memory_from_messages

_KEY_FINDING_RE = re.compile(r"<key_finding>(.*?)</key_finding>", re.DOTALL)
_CORRECTION_MARKERS = (
    "不对", "不是", "应该", "以后", "记住", "不要", "别再", "改成",
    "注意", "错了", "正确的是", "别用", "改用",
)
_ALLOWED_TYPES = CORE_TYPES | {"feedback"}
_ALLOWED_SCOPES = {"user", "project", "skill"}
_ALLOWED_CONF = {"high", "medium", "low"}
_ALLOWED_SENS = {"L1", "L2", "L3"}


def _text(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    return str(content or "")


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def clean_messages(messages: list[dict]) -> list[dict]:
    """去掉运行时 memory_context，避免把注入内容再蒸馏回记忆。"""
    return strip_memory_from_messages(messages or [])


def episode_body(segment: list[dict], step_results: list[dict] | None = None) -> str:
    """从一段执行历史生成可检索的 episode 正文（规则化，零 LLM）。"""
    findings: list[str] = []
    recent: list[str] = []
    corrections: list[str] = []
    actions: list[str] = []
    for msg in segment or []:
        role = msg.get("role")
        text = _text(msg).strip()
        if not text:
            continue
        if role == "assistant":
            for hit in _KEY_FINDING_RE.findall(text):
                hit = hit.strip()
                if hit and hit not in findings:
                    findings.append(hit)
            if text and not text.startswith("<condensed_history>"):
                snippet = re.sub(r"\s+", " ", text)[:300]
                if snippet and snippet not in recent:
                    recent.append(snippet)
        elif role == "user":
            if any(mark in text for mark in _CORRECTION_MARKERS) and len(text) <= 500:
                snippet = re.sub(r"\s+", " ", text)[:300]
                if snippet and snippet not in corrections:
                    corrections.append(snippet)
        elif role == "tool":
            name = str(msg.get("name") or "tool")
            if name not in actions:
                actions.append(name)
    for step in step_results or []:
        name = str(step.get("type") or "").strip()
        if name and name not in actions:
            actions.append(name)

    if not (findings or recent or corrections or actions):
        return ""
    parts = []
    if findings:
        parts.append("【关键结论】" + "；".join(findings[-5:]))
    if corrections:
        parts.append("【用户纠正】" + "；".join(corrections[-5:]))
    if recent:
        parts.append("【最近进展】" + "；".join(recent[-3:]))
    if actions:
        parts.append("【执行动作】" + "、".join(actions[-8:]))
    return "\n".join(parts)[:2000]


def rule_candidates(
    messages: list[dict],
    *,
    skill_name: str = "",
    session_id: str = "",
    step_results: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """零 LLM 候选：key_finding -> fact/decision；用户纠正 -> feedback。"""
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for msg in messages or []:
        text = _text(msg).strip()
        if not text:
            continue
        for hit in _KEY_FINDING_RE.findall(text):
            hit = re.sub(r"\s+", " ", hit).strip()
            if not hit or contains_secret(hit):
                continue
            kind = "decision" if re.search(r"决定|采用|选择|方案|根因|结论", hit) else "fact"
            title = f"关键结论：{hit[:40]}"
            key = (kind, hit)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "type": kind,
                "title": title,
                "body": hit[:800],
                "scope": "project",
                "domain": [],
                "confidence": "high",
                "sensitivity": "L2",
                "source": f"distill:{session_id or skill_name}",
                "data": {"session_id": session_id, "skill": skill_name},
            })
        if (
            msg.get("role") == "user"
            and 10 <= len(text) <= 500
            and any(mark in text for mark in _CORRECTION_MARKERS)
            and not contains_secret(text)
        ):
            body = re.sub(r"\s+", " ", text).strip()
            title = f"用户纠正：{body[:40]}"
            key = ("feedback", body)
            if key not in seen:
                seen.add(key)
                candidates.append({
                    "type": "feedback",
                    "title": title,
                    "body": body,
                    "scope": "user",
                    "domain": [],
                    "confidence": "medium",
                    "sensitivity": "L2",
                    "source": f"distill:{session_id or skill_name}",
                    "data": {"session_id": session_id, "skill": skill_name},
                })
    return candidates


def _parse_llm_json(content: str) -> list[dict]:
    text = _strip_code_fences(content)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001 — 模型输出不可信，解析失败按空候选
        return []
    records = data.get("records") if isinstance(data, dict) else None
    return records if isinstance(records, list) else []


def llm_candidates(
    messages: list[dict],
    llm,
    *,
    skill_name: str = "",
    session_id: str = "",
    max_records: int = 8,
) -> list[dict[str, Any]]:
    """用便宜模型生成候选；只返回通过校验的 dict，不落库。"""
    lines: list[str] = []
    for msg in messages or []:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = re.sub(r"\s+", " ", _text(msg)).strip()
        if not text or text.startswith("[进度]"):
            continue
        lines.append(f"[{role}] {text[:1200]}")
    transcript = "\n".join(lines[-40:])[:12000]
    if len(transcript) < 40:
        return []
    prompt = f"""你是长期记忆蒸馏器。把下面会话中“以后仍然有用”的信息提炼为最多 {max_records} 条记忆。

只输出 JSON：
{{"records":[{{
  "type": "fact|preference|decision|feedback|term|resource|state|goal|task|project|phase",
  "title": "≤80字",
  "body": "≤800字，一条只写一件事",
  "scope": "user|project|skill",
  "domain": ["coding"],
  "entity": "可选实体名",
  "valid_until": "可选 ISO 时间，仅 state/observation",
  "confidence": "high|medium|low",
  "sensitivity": "L1|L2|L3"
}}]}}

规则：
- 只提取跨轮仍成立的事实、决定、偏好、纠正、坑；不提取工具输出、临时命令、一次性路径、未验证猜测。
- 不编造；没有来源就不输出。不要输出密钥。
- 用户偏好/身份/资源/阶段/状态应使用 scope=user 或标记低置信度，等待人工评审。
- 正文不要复制大段原文。

会话 skill={skill_name} session={session_id}：
{transcript}
"""
    try:
        resp = llm.invoke(prompt)
    except Exception:  # noqa: BLE001 — 蒸馏失败不能影响会话退出
        return []
    content = resp if isinstance(resp, str) else getattr(resp, "content", str(resp))
    out: list[dict[str, Any]] = []
    for item in _parse_llm_json(str(content or "")):
        if not isinstance(item, dict):
            continue
        type_ = str(item.get("type") or "fact").strip().lower()
        title = str(item.get("title") or "").strip()
        body = str(item.get("body") or "").strip()
        if type_ not in _ALLOWED_TYPES or not title or not body:
            continue
        if len(title) > 80 or len(body) > 800:
            continue
        if contains_secret(title + "\n" + body):
            continue
        scope = str(item.get("scope") or "project").strip().lower()
        confidence = str(item.get("confidence") or "medium").strip().lower()
        sensitivity = str(item.get("sensitivity") or "L2").strip().upper()
        out.append({
            "type": type_,
            "title": title,
            "body": body,
            "scope": scope if scope in _ALLOWED_SCOPES else "project",
            "domain": item.get("domain") if isinstance(item.get("domain"), list) else [],
            "subject": str(item.get("entity") or "").strip(),
            "valid_until": str(item.get("valid_until") or "").strip(),
            "confidence": confidence if confidence in _ALLOWED_CONF else "medium",
            "sensitivity": sensitivity if sensitivity in _ALLOWED_SENS else "L2",
            "source": f"llm-distill:{session_id or skill_name}",
            "data": {"session_id": session_id, "skill": skill_name},
        })
    return out[:max_records]


def merge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 type/title/body 去重，保留先出现的（规则候选优先）。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for c in candidates or []:
        key = (
            str(c.get("type") or ""),
            str(c.get("title") or "").strip(),
            str(c.get("body") or "").strip(),
        )
        if not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out
