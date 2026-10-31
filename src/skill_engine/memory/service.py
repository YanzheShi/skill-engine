"""MemoryService：长期记忆的统一读写门面。

职责：
- 记录写入/查询/遗忘/更新；
- user/project 双 store 与全局 FTS 索引；
- 实体/资源精确解析（路径存在性校验）；
- PROFILE/NOW/PROJECT 视图渲染；
- 项目初始化候选生成。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .index import MemoryIndex
from .models import (
    CONFIDENCE_RANK,
    ENTITY_TYPES,
    MemoryHit,
    MemoryRecord,
    make_id,
    now_iso,
)
from .settings import MemoryConfig, load_memory_config
from .store import MemoryStore, valid_id
from .views import render_context, render_now, render_profile

logger = logging.getLogger(__name__)


def _safe_read(path: Path, limit: int = 4000) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except (OSError, UnicodeError):
        return ""


def _git_remote(root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return (proc.stdout or "").strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def derive_project_id(root: Path | str) -> str:
    root = Path(root).expanduser().resolve()
    remote = _git_remote(root)
    seed = remote.lower() if remote else str(root).lower()
    return "proj-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _norm_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()]


def _read_pyproject(path: Path) -> dict[str, str]:
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        project = data.get("project") if isinstance(data, dict) else None
        if not isinstance(project, dict):
            return {}
        return {
            "name": str(project.get("name") or "").strip(),
            "description": str(project.get("description") or "").strip(),
            "requires_python": str(project.get("requires-python") or "").strip(),
        }
    except (OSError, ValueError, TypeError):
        return {}


class MemoryService:
    """一个工作根上下文中的长期记忆服务。"""

    def __init__(
        self,
        working_root: Path | str | None = None,
        *,
        config: MemoryConfig | None = None,
        project_id: str | None = None,
        user_store: Path | str | None = None,
        project_store: Path | str | None = None,
        index_path: Path | str | None = None,
    ):
        self.config = config or load_memory_config()
        self.working_root = Path(working_root or os.getcwd()).expanduser().resolve()
        self.project_id = project_id or derive_project_id(self.working_root)
        self.user_store = MemoryStore(
            Path(user_store) if user_store else self.config.user_store,
            "user",
        )
        self.project_store = MemoryStore(
            Path(project_store) if project_store else self.config.project_store(self.working_root),
            "project",
            self.project_id,
        )
        # 每个工作根一个索引：收录当前 user store + 当前 project store。
        # 不用单一全局索引，避免在项目 B 重建时把项目 A 的索引行冲掉。
        self._index_path = (
            Path(index_path) if index_path
            else self.project_store.root / ".index.sqlite"
        )
        self._index: MemoryIndex | None = None
        self._dirty = False

    # ---- 生命周期/状态 ----

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def set_enabled(self, value: bool) -> None:
        self.config.enabled = bool(value)

    def close(self) -> None:
        if self._index is not None:
            self._index.close()
            self._index = None

    def _stores(self) -> list[MemoryStore]:
        return [self.user_store, self.project_store]

    def _all_records(self) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        seen: set[tuple[str, str]] = set()
        for store in self._stores():
            for record in store.list_records():
                key = (store.store_scope, record.id)
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
        records.sort(key=lambda r: (r.updated_at or "", r.id), reverse=True)
        return records

    def _get_index(self, *, rebuild_if_empty: bool = True) -> MemoryIndex | None:
        if not self.enabled or not self.config.fts_enabled:
            return None
        if self._index is None:
            try:
                self._index = MemoryIndex(self._index_path)
            except Exception as e:  # noqa: BLE001
                logger.warning("记忆索引初始化失败，回退文件检索: %s", e)
                self.config.fts_enabled = False
                return None
        if rebuild_if_empty:
            # 用记录数 + 最新 mtime 的轻量指纹判断外部变更；只有变化时才读全文重建。
            signature = self._signature()
            if self._dirty or self._index.get_meta("signature") != signature:
                self._index.rebuild(self._all_records())
                self._index.set_meta("signature", signature)
                self._dirty = False
        return self._index

    def _signature(self) -> str:
        return json.dumps(
            [self.user_store.signature(), self.project_store.signature()],
            separators=(",", ":"),
        )

    def reindex(self) -> dict[str, Any]:
        index = self._get_index(rebuild_if_empty=False)
        records = self._all_records()
        if index is None:
            return {"status": "disabled", "records": len(records)}
        index.rebuild(records)
        index.set_meta("signature", self._signature())
        self._dirty = False
        return {"status": "ok", "records": len(records), **index.stats()}

    # ---- 写入 ----

    def _existing_ids(self) -> set[str]:
        return {r.id for r in self._all_records()}

    def _find_by_hash(self, digest: str) -> MemoryRecord | None:
        for record in self._all_records():
            if record.content_hash == digest:
                return record
        return None

    def add(
        self,
        *,
        type: str = "fact",
        title: str,
        body: str = "",
        scope: str = "project",
        domain: Any = None,
        tags: Any = None,
        subject: str = "",
        relations: Any = None,
        valid_from: str = "",
        valid_until: str = "",
        observed_at: str = "",
        confidence: str = "medium",
        importance: float = 0.5,
        sensitivity: str = "L2",
        source: str = "user",
        pinned: bool = False,
        data: dict[str, Any] | None = None,
        status: str = "active",
        record_id: str = "",
    ) -> MemoryRecord:
        if not self.enabled:
            raise RuntimeError("memory is disabled")
        title = (title or "").strip()
        body = (body or "").strip()
        if not title:
            raise ValueError("title is required")
        if len(body) > self.config.max_body_chars:
            raise ValueError(
                f"body too long ({len(body)} > {self.config.max_body_chars}); "
                "write a file and store a pointer instead"
            )
        payload = json.dumps([title, body, data or {}], ensure_ascii=False, default=str)
        from .redact import contains_secret

        if contains_secret(payload):
            raise ValueError("refusing to store secret-like content")

        scope = (scope or "project").strip().lower()
        if scope not in ("user", "project", "skill", "device", "session"):
            scope = "project"
        data = dict(data or {})
        if type in ("state", "observation") and not valid_until:
            days = int(data.get("ttl_days") or self.config.status_ttl_days)
            valid_until = (
                datetime.now(UTC) + timedelta(days=max(1, days))
            ).isoformat(timespec="seconds")

        record = MemoryRecord(
            id=record_id or make_id(type, title, self._existing_ids()),
            type=type,
            title=title,
            body=body,
            scope=scope,
            domain=_norm_list(domain),
            tags=_norm_list(tags),
            subject=subject or "",
            relations=list(relations or []),
            valid_from=valid_from or "",
            valid_until=valid_until or "",
            observed_at=observed_at or "",
            status=status,
            confidence=confidence,
            importance=importance,
            sensitivity=sensitivity,
            source=source,
            pinned=bool(pinned),
            data=data,
        )
        existing = self._find_by_hash(record.content_hash)
        if existing is not None:
            return existing

        store = self.user_store if scope in ("user", "skill", "device", "session") else self.project_store
        store.save(record)
        self._dirty = True
        return record

    def update(self, record_id: str, **changes: Any) -> MemoryRecord | None:
        if not valid_id(record_id):
            return None
        record = self.get(record_id, include_private=True)
        if record is None:
            return None
        allowed = {
            "title", "body", "type", "scope", "domain", "tags", "subject",
            "relations", "valid_from", "valid_until", "observed_at", "status",
            "confidence", "importance", "sensitivity", "source", "pinned", "data",
        }
        for key, value in changes.items():
            if key in allowed and value is not None:
                if key in ("domain", "tags"):
                    value = _norm_list(value)
                setattr(record, key, value)
        store = self.user_store if record.store_scope == "user" else self.project_store
        store.save(record)
        self._dirty = True
        return record

    def forget(self, record_id: str) -> bool:
        if not valid_id(record_id):
            return False
        for store in self._stores():
            if store.get(record_id) is not None:
                if not store.delete(record_id):
                    return False
                self._dirty = True
                return True
        return False

    # ---- 读取 ----

    def get(self, record_id: str, *, include_private: bool = False) -> MemoryRecord | None:
        if not valid_id(record_id):
            return None
        for store in self._stores():
            record = store.get(record_id)
            if record is not None:
                if record.sensitivity == "L3" and not include_private:
                    return None
                return record
        return None

    def list_records(self, scope: str = "all") -> list[MemoryRecord]:
        records = self._all_records()
        if scope == "user":
            return [r for r in records if r.store_scope == "user"]
        if scope == "project":
            return [r for r in records if r.store_scope == "project"]
        return records

    def search(
        self,
        query: str,
        *,
        scope: str = "auto",
        record_type: str = "",
        domain: str = "",
        as_of: str = "",
        top_k: int = 3,
        include_history: bool = False,
        include_private: bool = False,
    ) -> list[MemoryHit]:
        if not self.enabled:
            return []
        idx = self._get_index()
        preferred = "project" if self.project_store.exists() else "user"
        if idx is not None:
            return idx.search(
                query,
                scope=scope,
                project_id=self.project_id,
                preferred_scope=preferred,
                record_type=record_type,
                domain=domain,
                as_of=as_of or None,
                include_history=include_history,
                include_private=include_private,
                top_k=top_k,
            )
        return self._search_files(
            query,
            scope=scope,
            record_type=record_type,
            domain=domain,
            as_of=as_of,
            top_k=top_k,
            include_history=include_history,
            include_private=include_private,
            preferred=preferred,
        )

    def _search_files(
        self,
        query: str,
        *,
        scope: str,
        record_type: str,
        domain: str,
        as_of: str,
        top_k: int,
        include_history: bool,
        include_private: bool,
        preferred: str,
    ) -> list[MemoryHit]:
        q = (query or "").strip()
        ql = q.lower()
        hits: list[MemoryHit] = []
        for record in self._all_records():
            if not include_history and record.status != "active":
                continue
            if include_history:
                if not record.is_time_valid(as_of or None):
                    continue
            elif not record.is_current(as_of or None):
                continue
            if record.sensitivity == "L3" and not include_private:
                continue
            if record_type and record.type != record_type:
                continue
            if domain and domain not in record.domain:
                continue
            if scope == "user" and record.store_scope != "user":
                continue
            if scope == "project" and (
                record.store_scope != "project" or record.project_id != self.project_id
            ):
                continue
            hay = " ".join([
                record.title, record.body, " ".join(record.tags),
                " ".join(record.domain), record.path_value(),
                " ".join(record.aliases()),
            ]).lower()
            if ql and ql not in hay:
                continue
            text = 1.0 if ql and ql in record.title.lower() else (0.5 if ql else 0.3)
            exact = 1.0 if ql and ql == record.title.lower() else (
                0.8 if ql and ql in record.title.lower() else 0.0
            )
            scope_match = 1.0 if record.scope == preferred else (
                0.5 if record.scope == "user" else 0.2
            )
            score = (
                0.40 * text + 0.20 * exact + 0.15 * scope_match
                + 0.10 * 0.8 + 0.10 * (CONFIDENCE_RANK.get(record.confidence, 2) / 3.0)
                + 0.05 * record.importance
            )
            hits.append(MemoryHit(record=record, score=score, text_score=text))
        hits.sort(key=lambda h: (h.score, h.record.importance, h.record.updated_at), reverse=True)
        return hits[: max(1, int(top_k))]

    # ---- 视图/注入 ----

    def render_context(self, *, skill_name: str = "", include_private: bool = False) -> str:
        if not self.enabled:
            return ""
        return render_context(
            self._all_records(),
            self.config,
            project_id=self.project_id,
            skill_name=skill_name,
            include_private=include_private,
        )

    def build_session_memory_message(self, *, skill_name: str = "",
                                     include_private: bool = False) -> dict | None:
        block = self.render_context(skill_name=skill_name, include_private=include_private)
        return {"role": "user", "content": block} if block else None

    def render_profile(self, *, include_private: bool = False) -> str:
        text, _ = render_profile(
            self._all_records(), budget=self.config.profile_budget,
            include_private=include_private,
        )
        return text

    def render_now(self, *, as_of: str = "", include_private: bool = False) -> str:
        text, _ = render_now(
            self._all_records(), budget=self.config.now_budget, as_of=as_of or None,
            include_private=include_private,
        )
        return text

    # ---- 实体/资源精确解析 ----

    def resolve_entity(self, name: str) -> dict[str, Any]:
        q = (name or "").strip()
        if not q:
            return {"status": "not_found", "candidates": []}
        ql = q.casefold()
        candidates = []
        resolvable = ENTITY_TYPES | {"term"}
        for record in self._all_records():
            if record.status != "active" or record.type not in resolvable:
                continue
            names = [record.title, record.name_value(), record.path_value()] + record.aliases()
            names = [str(n).casefold() for n in names if n]
            if ql in names:
                score = 1.0
            elif any(ql in n for n in names):
                score = 0.7
            else:
                score = 0.0
            if score <= 0:
                continue
            candidates.append({
                "id": record.id,
                "title": record.title,
                "type": record.type,
                "scope": record.scope,
                "status": record.status,
                "path": record.path_value(),
                "aliases": record.aliases(),
                "score": score,
            })
        candidates.sort(key=lambda c: (-c["score"], c["title"]))
        if not candidates:
            return {"status": "not_found", "query": q, "candidates": []}
        if len(candidates) == 1 or candidates[0]["score"] == 1.0 and candidates[1]["score"] < 1.0:
            status = "ok"
        else:
            status = "ambiguous"
        return {"status": status, "query": q, "candidates": candidates[:10]}

    def resolve_resource(self, name: str) -> dict[str, Any]:
        q = (name or "").strip()
        if not q:
            return {"status": "not_found", "candidates": []}
        ql = q.casefold()
        candidates = []
        for record in self._all_records():
            if record.status != "active" or record.type != "resource":
                continue
            path = record.path_value()
            names = [record.title, record.name_value(), path] + record.aliases()
            names = [str(n).casefold() for n in names if n]
            if ql in names:
                score = 1.0
            elif any(ql in n for n in names):
                score = 0.7
            else:
                score = 0.0
            if score <= 0:
                continue
            full = self._resolve_resource_path(path)
            candidates.append({
                "id": record.id,
                "title": record.title,
                "scope": record.scope,
                "path": str(full) if full else path,
                "exists": bool(full and full.exists()),
                "kind": record.data.get("kind", ""),
                "last_verified": record.data.get("verified_at", ""),
                "aliases": record.aliases(),
                "score": score,
            })
        candidates.sort(key=lambda c: (-c["score"], c["title"]))
        if not candidates:
            return {"status": "not_found", "query": q, "candidates": []}
        if len(candidates) > 1 and (
            candidates[0]["score"] < 1.0 or candidates[1]["score"] >= 1.0
        ):
            return {"status": "ambiguous", "query": q, "candidates": candidates[:10]}
        best = candidates[0]
        status = "ok" if best["exists"] else "missing_path"
        return {"status": status, "query": q, "candidate": best, "candidates": candidates[:10]}

    def _resolve_resource_path(self, path: str) -> Path | None:
        if not path:
            return None
        p = Path(os.path.expandvars(path)).expanduser()
        if not p.is_absolute():
            p = self.working_root / p
        return p

    # ---- 阶段/项目治理 ----

    def phase_start(self, title: str, *, domain: Any = None, body: str = "") -> MemoryRecord:
        return self.add(
            type="phase", title=title, body=body, scope="user",
            domain=domain or ["planning"],
            data={"start": now_iso()[:10]},
            confidence="high", importance=0.8, source="user",
        )

    def phase_end(self, record_id: str) -> MemoryRecord | None:
        record = self.get(record_id, include_private=True)
        if record is None or record.type != "phase":
            return None
        data = dict(record.data)
        data["end"] = now_iso()[:10]
        return self.update(record_id, status="archived", data=data)

    def project_init(self, root: Path | str | None = None, *, dry_run: bool = True) -> list[Any]:
        target = Path(root or self.working_root).expanduser().resolve()
        svc = self if target == self.working_root else MemoryService(target, config=self.config)
        candidates = svc._collect_project_records(target)
        if dry_run:
            return candidates
        saved = []
        for candidate in candidates:
            try:
                saved.append(svc.add(**candidate))
            except Exception as e:  # noqa: BLE001
                logger.warning("项目初始化跳过一条记录: %s", e)
        return saved

    def _collect_project_records(self, root: Path) -> list[dict[str, Any]]:
        remote = _git_remote(root)
        readme = _safe_read(root / "README.md", 4000)
        body = readme.strip()[:800]
        records: list[dict[str, Any]] = [
            {
                "record_id": f"project-{self.project_id}",
                "type": "project",
                "title": root.name,
                "body": body or f"项目根目录：{root}",
                "scope": "project",
                "domain": ["coding"],
                "data": {
                    "path": str(root),
                    "remote": remote,
                    "aliases": [root.name],
                },
                "pinned": True,
                "importance": 0.9,
                "confidence": "high",
                "sensitivity": "L2",
                "source": "project-init",
            }
        ]
        meta = _read_pyproject(root / "pyproject.toml")
        if meta.get("name") or meta.get("description"):
            records.append({
                "record_id": f"fact-{self.project_id}-metadata",
                "type": "fact",
                "title": "Project metadata",
                "body": "; ".join(
                    f"{k}={v}" for k, v in meta.items() if v
                ),
                "scope": "project",
                "domain": ["coding"],
                "data": {"path": str(root / "pyproject.toml")},
                "importance": 0.6,
                "confidence": "high",
                "sensitivity": "L2",
                "source": "project-init",
            })
        agents = root / "AGENTS.md"
        if agents.exists():
            records.append({
                "record_id": f"reference-{self.project_id}-agents",
                "type": "reference",
                "title": "Project instructions",
                "body": "AGENTS.md 是权威指令；需要时直接读取该文件，不要把内容复制进记忆。",
                "scope": "project",
                "domain": ["coding"],
                "data": {"path": str(agents)},
                "importance": 0.7,
                "confidence": "high",
                "sensitivity": "L2",
                "source": "project-init",
            })
        return records

    def verify_resources(self) -> list[dict[str, Any]]:
        out = []
        for record in self._all_records():
            if record.type != "resource":
                continue
            path = record.path_value()
            full = self._resolve_resource_path(path)
            out.append({
                "id": record.id,
                "title": record.title,
                "path": str(full) if full else path,
                "exists": bool(full and full.exists()),
                "last_verified": record.data.get("verified_at", ""),
            })
        return out

    def stats(self) -> dict[str, Any]:
        records = self._all_records()
        by_type: dict[str, int] = {}
        for r in records:
            by_type[r.type] = by_type.get(r.type, 0) + 1
        index = self._get_index()
        return {
            "enabled": self.enabled,
            "working_root": str(self.working_root),
            "project_id": self.project_id,
            "user_store": str(self.user_store.root),
            "project_store": str(self.project_store.root),
            "records": len(records),
            "by_type": by_type,
            "index": index.stats() if index else {"fts_available": False},
            "config": self.config.to_dict(),
        }
