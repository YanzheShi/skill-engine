"""从现有 Markdown 记忆/日志导入候选（P2）。

导入只生成 pending 记录，不直接激活；用户通过 ``memory review`` 决定。
支持：
- 带 YAML frontmatter 的旧记忆（user/feedback/project/reference -> 新类型）；
- 无 frontmatter 的按日工作日志 -> episode。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .frontmatter import split_frontmatter
from .models import CORE_TYPES
from .redact import contains_secret

_SKIP_DIRS = {".git", ".venv", ".venv-test", "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache"}
_TYPE_MAP = {
    "user": "preference",
    "feedback": "feedback",
    "project": "fact",
    "reference": "reference",
}
_USER_TYPES = {"preference", "identity", "feedback", "reference", "term", "environment"}
_PROJECT_TYPES = {"fact", "decision", "project", "phase", "goal", "task", "resource"}


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            value = line.lstrip("#").strip()
            if value:
                return value[:80]
    return ""


def _files(paths) -> list[Path]:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(p.glob("*.md")))
    return out


def _files_recursive(paths) -> list[Path]:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            for child in sorted(p.rglob("*.md")):
                if any(part in _SKIP_DIRS for part in child.parts):
                    continue
                out.append(child)
    return out


def _clean_body(text: str, limit: int = 1800) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    return text[:limit]


def collect_import_candidates(paths, *, recursive: bool = False) -> list[dict[str, Any]]:
    files = _files_recursive(paths) if recursive else _files(paths)
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in files:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = split_frontmatter(raw)
        body = _clean_body(body or "")
        if meta:
            old_type = str(meta.get("type") or "").strip().lower()
            type_ = _TYPE_MAP.get(old_type, old_type if old_type in CORE_TYPES else "fact")
            title = str(
                meta.get("name") or meta.get("title") or _first_heading(body) or path.stem
            ).strip()[:80]
            if not body:
                body = _clean_body(str(meta.get("description") or meta.get("summary") or ""))
            scope = str(meta.get("scope") or "").strip().lower()
            if scope not in ("user", "project", "skill"):
                scope = "project" if type_ in _PROJECT_TYPES else "user"
            sensitivity = str(meta.get("sensitivity") or "L2").strip().upper()
            if sensitivity not in ("L1", "L2", "L3"):
                sensitivity = "L2"
            data: dict[str, Any] = {}
            if isinstance(meta.get("data"), dict):
                data.update(meta["data"])
            for key in ("aliases", "definition", "path", "name", "kind"):
                if meta.get(key) is not None and key not in data:
                    data[key] = meta[key]
            source = f"import:{path}"
        else:
            type_ = "episode"
            title = f"导入日志 {path.stem}"[:80]
            scope = "user"
            sensitivity = "L2"
            data = {"import_path": str(path)}
            source = f"import:{path}"

        if len(body) < 10 or not title:
            continue
        if contains_secret(title + "\n" + body):
            continue
        key = (type_, title, body)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "type": type_,
            "title": title,
            "body": body,
            "scope": scope,
            "domain": meta.get("domain") if isinstance(meta.get("domain"), list) else [],
            "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
            "confidence": "medium",
            "sensitivity": sensitivity,
            "source": source,
            "status": "pending",
            "data": data,
        })
    return candidates
