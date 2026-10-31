"""SQLite FTS5 索引（可重建，不是事实源）。

P1 的检索路径：
1. SQL 元数据过滤（scope/type/domain/status/sensitivity）；
2. FTS5 BM25 / trigram（中文子串），短查询回退 LIKE；
3. Python 轻量 rerank（精确匹配 / scope / 时效 / 置信度 / 重要度）。

不引入向量库；索引损坏可通过 ``memory reindex`` 从 Markdown 重建。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    CONFIDENCE_RANK,
    MemoryHit,
    MemoryRecord,
    _parse_dt,
)

logger = logging.getLogger(__name__)

_CANDIDATE_LIMIT = 500


class MemoryIndex:
    """一条全局索引，同时收录 user 与 project 记录。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        self.fts_available = self._init_schema()

    # ---- schema ----

    def _init_schema(self) -> bool:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
              id TEXT PRIMARY KEY,
              type TEXT NOT NULL,
              title TEXT NOT NULL,
              body TEXT NOT NULL DEFAULT '',
              tags TEXT NOT NULL DEFAULT '',
              domain TEXT NOT NULL DEFAULT '',
              scope TEXT NOT NULL DEFAULT 'project',
              status TEXT NOT NULL DEFAULT 'active',
              sensitivity TEXT NOT NULL DEFAULT 'L2',
              confidence TEXT NOT NULL DEFAULT 'medium',
              importance REAL NOT NULL DEFAULT 0.5,
              valid_from TEXT NOT NULL DEFAULT '',
              valid_until TEXT NOT NULL DEFAULT '',
              pinned INTEGER NOT NULL DEFAULT 0,
              store_scope TEXT NOT NULL DEFAULT 'user',
              project_id TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL DEFAULT '',
              record_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_records_lookup
              ON records(status, store_scope, type, valid_until);
            CREATE INDEX IF NOT EXISTS idx_records_project
              ON records(project_id, store_scope);
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL DEFAULT ''
            );
            """
        )
        fts = True
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5("
                "id UNINDEXED, title, body, tags, tokenize='trigram')"
            )
        except sqlite3.DatabaseError:
            try:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5("
                    "id UNINDEXED, title, body, tags)"
                )
            except sqlite3.DatabaseError as e:  # pragma: no cover - 取决于 SQLite 构建
                logger.warning("FTS5 不可用，记忆检索回退 LIKE: %s", e)
                fts = False
        self.conn.commit()
        return fts

    # ---- 写 ----

    def _insert(self, record: MemoryRecord) -> None:
        # 别名/实体名/路径也进检索文本，否则用户口语/别名只能靠
        # 精确解析，无法被 FTS 或 LIKE 命中。
        search_tags = record.tags + record.domain + record.aliases()
        if record.name_value():
            search_tags.append(record.name_value())
        if record.path_value():
            search_tags.append(record.path_value())
        search_text = ",".join(search_tags)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO records
              (id, type, title, body, tags, domain, scope, status, sensitivity,
               confidence, importance, valid_from, valid_until, pinned,
               store_scope, project_id, updated_at, record_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.id, record.type, record.title, record.body or "",
                search_text, ",".join(record.domain),
                record.scope, record.status, record.sensitivity,
                record.confidence, record.importance,
                record.valid_from or "", record.valid_until or "",
                1 if record.pinned else 0,
                record.store_scope, record.project_id,
                record.updated_at or "",
                json.dumps(record.to_dict(), ensure_ascii=False, default=str),
            ),
        )
        if self.fts_available:
            self.conn.execute("DELETE FROM records_fts WHERE id = ?", (record.id,))
            # trigram 对英文大小写敏感；索引和查询统一转小写，保持与 LIKE 一致。
            self.conn.execute(
                "INSERT INTO records_fts(id, title, body, tags) VALUES (?,?,?,?)",
                (record.id, record.title.lower(), (record.body or "").lower(),
                 search_text.lower()),
            )

    def upsert(self, record: MemoryRecord) -> None:
        self._insert(record)
        self.conn.commit()

    def delete(self, record_id: str) -> None:
        self.conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
        if self.fts_available:
            self.conn.execute("DELETE FROM records_fts WHERE id = ?", (record_id,))
        self.conn.commit()

    def rebuild(self, records: list[MemoryRecord]) -> None:
        self.conn.execute("DELETE FROM records")
        if self.fts_available:
            self.conn.execute("DELETE FROM records_fts")
        for record in records:
            self._insert(record)
        self.conn.commit()

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()
        return int(row["n"]) if row else 0

    def get_meta(self, key: str) -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else ""

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    # ---- 读 ----

    def _fts_query(self, query: str) -> str:
        return '"' + query.lower().replace('"', '""').replace("\n", " ") + '"'

    def _where(
        self,
        *,
        scope: str,
        project_id: str,
        record_type: str = "",
        domain: str = "",
        include_history: bool = False,
        include_private: bool = False,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if scope == "user":
            clauses.append("r.store_scope = 'user'")
        elif scope == "project":
            clauses.append("r.store_scope = 'project' AND r.project_id = ?")
            values.append(project_id)
        elif scope == "all":
            pass
        else:  # auto
            clauses.append(
                "(r.store_scope = 'user' OR (r.store_scope = 'project' AND r.project_id = ?))"
            )
            values.append(project_id)
        if record_type:
            clauses.append("r.type = ?")
            values.append(record_type)
        if domain:
            clauses.append("r.domain LIKE ?")
            values.append(f"%{domain}%")
        if not include_history:
            clauses.append("r.status = 'active'")
        if not include_private:
            clauses.append("r.sensitivity != 'L3'")
        return clauses, values

    def search(
        self,
        query: str,
        *,
        scope: str = "auto",
        project_id: str = "",
        preferred_scope: str = "project",
        record_type: str = "",
        domain: str = "",
        as_of: str | None = None,
        include_history: bool = False,
        include_private: bool = False,
        top_k: int = 3,
    ) -> list[MemoryHit]:
        q = (query or "").strip()
        clauses, values = self._where(
            scope=scope,
            project_id=project_id,
            record_type=record_type,
            domain=domain,
            include_history=include_history,
            include_private=include_private,
        )
        rows = []
        use_fts = self.fts_available and q and len(q) >= 3
        if use_fts:
            sql = (
                "SELECT r.*, bm25(records_fts) AS rank FROM records_fts f "
                "JOIN records r ON r.id = f.id WHERE records_fts MATCH ?"
            )
            params: list[Any] = [self._fts_query(q)] + values
            if clauses:
                sql += " AND " + " AND ".join(clauses)
            sql += f" ORDER BY rank LIMIT {_CANDIDATE_LIMIT}"
            try:
                rows = self.conn.execute(sql, params).fetchall()
            except sqlite3.DatabaseError as e:
                logger.debug("FTS 查询失败，回退 LIKE: %s", e)
                rows = []
        if not rows:
            if q:
                like = f"%{q}%"
                sql = (
                    "SELECT r.*, 0.0 AS rank FROM records r "
                    "WHERE (r.title LIKE ? OR r.body LIKE ? OR r.tags LIKE ?)"
                )
                params = [like, like, like] + values
            else:
                sql = "SELECT r.*, 0.0 AS rank FROM records r WHERE 1=1"
                params = list(values)
            if clauses:
                sql += " AND " + " AND ".join(clauses)
            sql += f" ORDER BY r.updated_at DESC LIMIT {_CANDIDATE_LIMIT}"
            try:
                rows = self.conn.execute(sql, params).fetchall()
            except sqlite3.DatabaseError as e:
                logger.warning("记忆索引查询失败: %s", e)
                return []

        pivot = _parse_dt(as_of) if as_of else datetime.now(UTC)
        if pivot is None:
            pivot = datetime.now(UTC)
        q_lower = q.lower()
        hits: list[MemoryHit] = []
        for row in rows:
            try:
                record = MemoryRecord.from_dict(json.loads(row["record_json"]))
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.debug("跳过无法解析的索引记录: %s", e)
                continue
            if not include_history and record.status != "active":
                continue
            if include_history:
                if not record.is_time_valid(pivot):
                    continue
            elif not record.is_current(pivot):
                continue
            record.store_scope = row["store_scope"]
            record.project_id = row["project_id"]

            rank = float(row["rank"] or 0.0)
            text_score = 1.0 / (1.0 + max(0.0, -rank)) if use_fts else (0.5 if q else 0.3)
            exact = 0.0
            if q_lower:
                names = [record.title, record.name_value(), record.path_value()] + record.aliases()
                names = [str(x).lower() for x in names if x]
                if any(q_lower == n for n in names):
                    exact = 1.0
                elif any(q_lower in n for n in names):
                    exact = 0.8
                elif any(q_lower in str(x).lower() for x in record.tags + record.domain):
                    exact = 0.5
            scope_match = 1.0 if record.scope == preferred_scope else (
                0.5 if record.scope == "user" else 0.2
            )
            updated = _parse_dt(record.updated_at)
            if updated:
                days = max(0.0, (pivot - updated).total_seconds() / 86400.0)
            else:
                days = 365.0
            recency = 1.0 / (1.0 + days / 30.0)
            confidence = CONFIDENCE_RANK.get(record.confidence, 2) / 3.0
            score = (
                0.40 * text_score
                + 0.20 * exact
                + 0.15 * scope_match
                + 0.10 * recency
                + 0.10 * confidence
                + 0.05 * record.importance
            )
            hits.append(MemoryHit(record=record, score=score, text_score=text_score))

        # 同一 id 去重；稳定排序
        dedup: dict[str, MemoryHit] = {}
        for hit in hits:
            old = dedup.get(hit.record.id)
            if old is None or hit.score > old.score:
                dedup[hit.record.id] = hit
        ordered = sorted(
            dedup.values(),
            key=lambda h: (h.score, h.record.importance, h.record.updated_at),
            reverse=True,
        )
        return ordered[: max(1, int(top_k))]

    def stats(self) -> dict[str, Any]:
        return {
            "db": str(self.db_path),
            "count": self.count(),
            "fts_available": self.fts_available,
        }

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except sqlite3.Error as e:
            logger.debug("关闭记忆索引失败: %s", e)
