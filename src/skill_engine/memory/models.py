"""长期记忆的通用记录模型。

设计约束：
- 结构只固定所有场景都成立的维度（scope/type/entity/time/source/confidence/
  importance/sensitivity/status），场景用 ``domain`` / ``tags`` 扩展；
- 记录是事实源，视图（PROFILE/NOW/...）只是查询结果；
- 类型专属字段放在 ``data``，未知 frontmatter 顶层字段也归入 ``data``。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime, time
from typing import Any

# 核心类型是“默认行为”的提示，不是数据库枚举；未知类型按 fact 处理。
CORE_TYPES = {
    "identity", "environment", "preference",
    "fact", "decision", "term", "observation", "state",
    "person", "organization", "place", "device", "service",
    "resource", "project", "phase", "goal", "task", "event", "routine",
    "episode",
}

ENTITY_TYPES = {
    "person", "organization", "place", "device", "service",
    "resource", "project",
}

PROFILE_TYPES = {"identity", "environment", "preference"}
NOW_TYPES = {"phase", "goal", "task", "event", "state"}
HISTORY_TYPES = {"episode"}

SCOPES = {"user", "project", "skill", "device", "session"}
SENSITIVITIES = {"L1", "L2", "L3"}
CONFIDENCES = {"high", "medium", "low"}
STATUSES = {"active", "pending", "stale", "superseded", "archived"}

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_dt(value: Any, *, end_of_day: bool = False) -> datetime | None:
    """把 ISO 日期/时间解析成带时区的 datetime；解析失败返回 None。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.max if end_of_day else time.min)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                d = date.fromisoformat(text[:10])
                dt = datetime.combine(d, time.max if end_of_day else time.min)
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)  # 无时区值按 UTC 解释
    return dt


def _clean_summary(text: str, limit: int = 160) -> str:
    text = re.sub(r"```.*?```", " ", text or "", flags=re.DOTALL)
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


@dataclass
class MemoryRecord:
    """一条通用记忆记录。"""

    id: str
    type: str
    title: str
    body: str = ""
    scope: str = "project"
    domain: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    subject: str = ""
    relations: list[dict] = field(default_factory=list)
    valid_from: str = ""
    valid_until: str = ""
    observed_at: str = ""
    status: str = "active"
    confidence: str = "medium"
    importance: float = 0.5
    sensitivity: str = "L2"
    source: str = ""
    pinned: bool = False
    supersedes: str = ""
    created_at: str = ""
    updated_at: str = ""
    content_hash: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    # 以下是运行时字段，不写入 frontmatter
    path: str = ""
    store_scope: str = "user"
    project_id: str = ""

    def __post_init__(self) -> None:
        self.type = (self.type or "fact").strip().lower()
        self.scope = (self.scope or "project").strip().lower()
        if self.scope not in SCOPES:
            self.scope = "project"
        self.status = (self.status or "active").strip().lower()
        if self.status not in STATUSES:
            self.status = "active"
        self.confidence = (self.confidence or "medium").strip().lower()
        if self.confidence not in CONFIDENCES:
            self.confidence = "medium"
        self.sensitivity = (self.sensitivity or "L2").strip().upper()
        if self.sensitivity not in SENSITIVITIES:
            self.sensitivity = "L2"
        try:
            self.importance = max(0.0, min(1.0, float(self.importance)))
        except (TypeError, ValueError):
            self.importance = 0.5
        if isinstance(self.domain, str):
            self.domain = [x.strip() for x in self.domain.split(",") if x.strip()]
        if isinstance(self.tags, str):
            self.tags = [x.strip() for x in self.tags.split(",") if x.strip()]
        if not isinstance(self.data, dict):
            self.data = {}
        if not self.created_at:
            self.created_at = now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.content_hash:
            self.content_hash = self.compute_hash()

    # ---- 展示/解析辅助 ----

    def summary(self, limit: int = 160) -> str:
        return (
            _clean_summary(self.body, limit)
            or self.data.get("description", "")
            or self.data.get("definition", "")
            or self.title
        )

    def aliases(self) -> list[str]:
        raw = self.data.get("aliases") or self.data.get("alias") or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        return [str(x).strip() for x in raw if str(x).strip()]

    def path_value(self) -> str:
        for key in ("path", "dir", "directory", "file"):
            value = self.data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def name_value(self) -> str:
        value = self.data.get("name")
        return str(value).strip() if value else self.title

    def is_time_valid(self, as_of: Any = None) -> bool:
        """只判断时间窗口，不判断 status。"""
        pivot = _parse_dt(as_of) if as_of is not None else datetime.now(UTC)
        if pivot is None:
            pivot = datetime.now(UTC)
        start = _parse_dt(self.valid_from)
        end = _parse_dt(self.valid_until, end_of_day=True)
        if start and pivot < start:
            return False
        return not (end and pivot > end)

    def is_current(self, as_of: Any = None) -> bool:
        """status=active 且处于时间有效期内。"""
        if self.status != "active":
            return False
        return self.is_time_valid(as_of)

    def compute_hash(self) -> str:
        stable_data = str(
            self.data.get("path") or self.data.get("name") or ""
        ) if isinstance(self.data, dict) else ""
        payload = "\n".join([
            self.type,
            self.title.strip(),
            self.body.strip(),
            self.scope,
            ",".join(sorted(self.domain)),
            stable_data,
        ])
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        out = {}
        for f in fields(self):
            out[f.name] = getattr(self, f.name)
        out["content_hash"] = self.content_hash or self.compute_hash()
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryRecord:
        allowed = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**clean)


def make_id(record_type: str, title: str, existing: set[str] | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40]
    if not slug:
        slug = "note"
    base = f"{(record_type or 'fact').lower()}-{slug}"
    for _ in range(20):
        candidate = f"{base}-{uuid.uuid4().hex[:8]}"
        if not existing or candidate not in existing:
            return candidate
    return f"{base}-{uuid.uuid4().hex[:12]}"


@dataclass
class MemoryHit:
    record: MemoryRecord
    score: float = 0.0
    text_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        r = self.record
        return {
            "id": r.id,
            "type": r.type,
            "title": r.title,
            "snippet": r.summary(180),
            "scope": r.scope,
            "domain": r.domain,
            "status": r.status,
            "valid_until": r.valid_until,
            "confidence": r.confidence,
            "importance": r.importance,
            "sensitivity": r.sensitivity,
            "source": r.source,
            "score": round(self.score, 4),
        }
