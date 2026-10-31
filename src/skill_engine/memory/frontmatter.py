"""Memory record 的 Markdown + YAML frontmatter 读写。

约定：
- 文件正文是 Markdown；frontmatter 承载可查询字段。
- 未知的 frontmatter 顶层字段会并入 ``data``，因此新增类型不需要改解析器。
- 运行时字段 ``path`` / ``store_scope`` / ``project_id`` 不落盘。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import MemoryRecord

_RUNTIME_FIELDS = {"path", "store_scope", "project_id"}

# 这些字段即使用空值也不影响可读性；其余空值省略，保持文件干净。
_ALWAYS = {
    "id", "type", "title", "scope", "status", "confidence",
    "importance", "sensitivity", "source", "pinned",
    "created_at", "updated_at", "content_hash",
}
_OPTIONAL = {
    "domain", "tags", "subject", "relations", "valid_from", "valid_until",
    "observed_at", "supersedes", "data",
}


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆分 YAML frontmatter 与正文。没有 frontmatter 时返回 ({}, text)。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def record_to_frontmatter(record: MemoryRecord) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in MemoryRecord.__dataclass_fields__.values():
        name = f.name
        if name in _RUNTIME_FIELDS:
            continue
        value = getattr(record, name)
        if name == "data":
            # 类型专属字段已经是 data；额外保留未知字段由 from_meta 处理。
            if value:
                out[name] = value
            continue
        if name in _ALWAYS or name in _OPTIONAL and value not in ("", None, [], {}):
            out[name] = value
    return out


def dump_record(record: MemoryRecord) -> str:
    meta = record_to_frontmatter(record)
    fm = yaml.safe_dump(
        meta, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).strip()
    body = (record.body or "").rstrip()
    return f"---\n{fm}\n---\n\n{body}\n"


def from_meta(meta: dict[str, Any], body: str, *, path: str = "",
              store_scope: str = "user", project_id: str = "") -> MemoryRecord:
    """把 frontmatter dict + body 还原为 MemoryRecord。

    未识别的顶层字段（例如旧文件的 start/end/goals）并入 data，兼容手工编辑。
    """
    known = set(MemoryRecord.__dataclass_fields__) | _RUNTIME_FIELDS
    data = dict(meta.get("data") or {}) if isinstance(meta.get("data"), dict) else {}
    for key, value in meta.items():
        if key not in known and key != "data":
            data.setdefault(key, value)
    clean = {k: v for k, v in meta.items() if k in known}
    clean["body"] = body
    clean["data"] = data
    clean["path"] = path
    clean["store_scope"] = store_scope
    clean["project_id"] = project_id
    if not clean.get("id"):
        clean["id"] = Path(path).stem if path else ""
    if not clean.get("title"):
        clean["title"] = clean.get("id", "untitled")
    return MemoryRecord.from_dict(clean)


def load_record(path: Path, *, store_scope: str, project_id: str = "") -> MemoryRecord:
    text = Path(path).read_text(encoding="utf-8")
    meta, body = split_frontmatter(text)
    return from_meta(meta, body, path=str(path),
                     store_scope=store_scope, project_id=project_id)
