"""按作用域分库的 Markdown 记忆存储。

- 每条记录一个 ``records/<id>.md`` 文件；Markdown 是事实源。
- 写入使用临时文件 + os.replace，避免半写损坏。
- 单写锁：线程锁 + 锁文件（跨进程尽力而为），写操作串行。
- 审计事件追加到 ``events.jsonl``，供 ``memory forget`` 后追溯。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .frontmatter import dump_record, load_record
from .models import MemoryRecord, now_iso
from .redact import redact_secrets

logger = logging.getLogger(__name__)

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_GUARD = threading.Lock()
_ID_RE = re.compile(r"^[^\x00-\x1f/\\]{1,200}$")


def valid_id(record_id: str) -> bool:
    """记录 ID 禁止路径分隔符/控制字符，防止 ../ 路径逃逸；允许中文。"""
    return bool(
        record_id
        and _ID_RE.fullmatch(record_id)
        and ".." not in record_id
        and record_id not in (".", "..")
    )


def _thread_lock_for(root: Path) -> threading.RLock:
    key = str(root.resolve()) if root.exists() else str(root)
    with _THREAD_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


class MemoryStore:
    """一个作用域（user 或 project）的 Memory Store。"""

    def __init__(self, root: Path | str, store_scope: str = "user", project_id: str = ""):
        self.root = Path(root).expanduser()
        self.store_scope = store_scope
        self.project_id = project_id if store_scope == "project" else ""
        self.records_dir = self.root / "records"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / ".lock"
        self._lock = _thread_lock_for(self.root)

    # ---- 目录/锁 ----

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.records_dir.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.records_dir.is_dir()

    def signature(self) -> tuple[int, int]:
        """轻量变更指纹：记录数 + 最新 mtime，不解析文件。"""
        if not self.records_dir.is_dir():
            return (0, 0)
        count = 0
        latest = 0
        try:
            with os.scandir(self.records_dir) as entries:
                for entry in entries:
                    if not entry.is_file() or not entry.name.endswith(".md"):
                        continue
                    count += 1
                    try:
                        latest = max(latest, entry.stat().st_mtime_ns)
                    except OSError:
                        continue
        except OSError:
            return (0, 0)
        return (count, latest)

    @contextmanager
    def lock(self, timeout: float = 5.0) -> Iterator[None]:
        if not self._lock.acquire(timeout=timeout):
            raise TimeoutError(f"memory store busy: {self.root}")
        fh = None
        try:
            self.ensure()
            deadline = time.time() + timeout
            while True:
                try:
                    fd = os.open(
                        str(self.lock_path),
                        os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    )
                    fh = os.fdopen(fd, "w", encoding="utf-8")
                    fh.write(f"{os.getpid()}\n")
                    fh.flush()
                    break
                except FileExistsError:
                    try:
                        if time.time() - self.lock_path.stat().st_mtime > 120:
                            self.lock_path.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    if time.time() >= deadline:
                        raise TimeoutError(f"memory store lock timeout: {self.root}")
                    time.sleep(0.05)
            yield
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError as e:
                    logger.debug("关闭记忆锁文件失败: %s", e)
                try:
                    self.lock_path.unlink(missing_ok=True)
                except OSError as e:
                    logger.debug("删除记忆锁文件失败: %s", e)
            self._lock.release()

    # ---- 读 ----

    def list_records(self) -> list[MemoryRecord]:
        if not self.records_dir.is_dir():
            return []
        out: list[MemoryRecord] = []
        for path in sorted(self.records_dir.glob("*.md")):
            try:
                out.append(load_record(
                    path,
                    store_scope=self.store_scope,
                    project_id=self.project_id,
                ))
            except Exception as e:  # noqa: BLE001
                logger.warning("跳过损坏的记忆文件 %s: %s", path, e)
        out.sort(key=lambda r: (r.updated_at or "", r.id), reverse=True)
        return out

    def get(self, record_id: str) -> MemoryRecord | None:
        if not valid_id(record_id):
            return None
        path = self.records_dir / f"{record_id}.md"
        if path.exists():
            try:
                return load_record(path, store_scope=self.store_scope,
                                   project_id=self.project_id)
            except Exception as e:  # noqa: BLE001 — 损坏文件不应阻断其他记忆
                logger.debug("读取记忆文件失败 %s: %s", path, e)
                return None
        # 兼容手工命名/导入的文件：按 frontmatter id 查找
        for record in self.list_records():
            if record.id == record_id:
                return record
        return None

    # ---- 写 ----

    def save(self, record: MemoryRecord) -> MemoryRecord:
        if not valid_id(record.id):
            raise ValueError(f"invalid memory id: {record.id!r}")
        record.store_scope = self.store_scope
        record.project_id = self.project_id
        record.updated_at = now_iso()
        if not record.created_at:
            record.created_at = record.updated_at
        record.content_hash = record.compute_hash()
        with self.lock():
            self.ensure()
            path = self.records_dir / f"{record.id}.md"
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(dump_record(record), encoding="utf-8")
            os.replace(tmp, path)
            record.path = str(path)
            self._append_event("upsert", record.id, {
                "type": record.type,
                "scope": record.scope,
                "status": record.status,
            })
        return record

    def delete(self, record_id: str) -> bool:
        if not valid_id(record_id):
            return False
        with self.lock():
            self.ensure()
            path = self.records_dir / f"{record_id}.md"
            if not path.exists():
                for record in self.list_records():
                    if record.id == record_id:
                        path = Path(record.path)
                        break
                else:
                    return False
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            self._append_event("forget", record_id, {"path": str(path)})
            return True

    def audit(self, action: str, record_id: str = "", detail: dict | None = None) -> None:
        with self.lock():
            self.ensure()
            self._append_event(action, record_id, detail)

    def _append_event(self, action: str, record_id: str, detail: dict | None) -> None:
        try:
            line = json.dumps({
                "at": now_iso(),
                "action": action,
                "id": record_id,
                "detail": redact_secrets(json.dumps(detail or {}, ensure_ascii=False)),
            }, ensure_ascii=False)
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            logger.debug("写入记忆审计事件失败: %s", e)
