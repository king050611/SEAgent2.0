"""
db/store.py – DBStateStore: PostgreSQL-backed state storage compatible with
the original StateStore interface (drop-in adapter). Supports the same
get_task/save_task/update_task_atomic/list_tasks/delete_task so the rest of
the code works transparently with either JSON files or the database.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session, sessionmaker
    from .models import Base, Task, Subtask, AuditLog, CacheMeta
    HAS_SQLALCHEMY = True
except Exception:  # pragma: no cover - optional dependency
    create_engine = None  # type: ignore
    Session = None  # type: ignore
    sessionmaker = None  # type: ignore
    Base = Task = Subtask = AuditLog = CacheMeta = None  # type: ignore
    HAS_SQLALCHEMY = False

logger = logging.getLogger(__name__)


class DBStateStore:
    """PostgreSQL-backed state store. Mirrors the API of the JSON StateStore.

    When DATABASE_URL is not set, falls back to a local SQLite JSONB-compatible
    file in data/tasks.db so the system runs without external infra.
    """

    def __init__(self, storage_dir: Optional[str] = None, db_url: Optional[str] = None):
        if not HAS_SQLALCHEMY:
            raise RuntimeError("SQLAlchemy not installed. Run: pip install 'sqlalchemy>=2.0' 'psycopg2-binary>=2.9'")
        resolved_url = db_url or os.environ.get("DATABASE_URL")
        if resolved_url:
            self.engine = create_engine(resolved_url, pool_pre_ping=True, pool_size=20, max_overflow=30)
        else:
            # Offline fallback: local SQLite (for dev environments without PG)
            storage_dir = storage_dir or "data"
            Path(storage_dir).mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(f"sqlite:///{storage_dir}/tasks.db", future=True)
        self._SessionLocal = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self._lock = Lock()
        self._init_schema()

    # ---------- setup ----------
    def _init_schema(self):
        try:
            Base.metadata.create_all(self.engine)
        except Exception as exc:  # pragma: no cover
            logger.warning("DB schema init failed (safe if already exists): %s", exc)

    # ---------- helpers ----------
    @staticmethod
    def _row_to_task_dict(task_row: Task) -> Dict[str, Any]:
        return task_row.to_dict()

    def _get_task_row(self, session: Session, task_id: str) -> Optional[Task]:
        return (
            session.query(Task)
            .filter(Task.task_id == task_id)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )

    # ---------- public API (mirrors StateStore) ----------
    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._SessionLocal() as session:
            row = session.query(Task).filter(Task.task_id == task_id).one_or_none()
            return self._row_to_task_dict(row) if row else None

    def save_task(self, task_id: str, task_state: Dict[str, Any]) -> None:
        with self._lock, self._SessionLocal() as session:
            existing = self._get_task_row(session, task_id)
            if existing:
                session.delete(existing)
                session.commit()
            self._upsert_state(session, task_state)
            session.commit()

    def update_task_atomic(self, task_id: str, update_func: Callable[[Dict[str, Any]], None]) -> Optional[Dict[str, Any]]:
        """DB-native atomic update with row-level locking; still calls
        update_func on the dict shape to keep compatibility."""
        with self._lock, self._SessionLocal() as session:
            row = self._get_task_row(session, task_id)
            if row is None:
                return None
            state_dict = self._row_to_task_dict(row)
            # Call the user-provided mutator exactly once (in-memory)
            update_func(state_dict)
            # Write mutated state back
            self._upsert_state(session, state_dict, existing_row=row)
            session.commit()
            return state_dict

    def delete_task(self, task_id: str) -> None:
        with self._lock, self._SessionLocal() as session:
            row = session.query(Task).filter(Task.task_id == task_id).one_or_none()
            if row:
                session.delete(row)
                session.commit()

    def list_tasks(self) -> Dict[str, Any]:
        tasks: Dict[str, Any] = {}
        with self._SessionLocal() as session:
            for row in session.query(Task).order_by(Task.created_at.desc()).all():
                tasks[row.task_id] = self._row_to_task_dict(row)
        return tasks

    # ---------- audit ----------
    def write_audit(self, **kwargs: Any) -> None:
        try:
            with self._SessionLocal() as session:
                session.add(AuditLog(**{k: v for k, v in kwargs.items() if hasattr(AuditLog, k)}))
                session.commit()
        except Exception as exc:
            logger.warning("Failed to write audit log: %s", exc)

    # ---------- cache counters ----------
    def bump_cache_stat(self, layer: str, *, hit: bool) -> None:
        try:
            with self._SessionLocal() as session:
                row = session.query(CacheMeta).filter(CacheMeta.cache_layer == layer).one_or_none()
                if not row:
                    row = CacheMeta(cache_layer=layer, hits=0, misses=0)
                    session.add(row)
                if hit:
                    row.hits = (row.hits or 0) + 1
                else:
                    row.misses = (row.misses or 0) + 1
                row.updated_at = time.time()
                session.commit()
        except Exception as exc:
            logger.warning("Failed to bump cache stat: %s", exc)

    # ---------- internal ----------
    def _upsert_state(self, session: Session, state: Dict[str, Any], existing_row: Optional[Task] = None) -> None:
        task_id = state["task_id"]
        if existing_row is None:
            existing_row = self._get_task_row(session, task_id)
        if existing_row is None:
            t = Task.from_dict(state)
            session.add(t)
            session.flush()
            task_pk = t
        else:
            self._update_task_row(existing_row, state)
            task_pk = existing_row
            # Drop existing subtask rows, we rebuild from list
            for s in existing_row.subtasks_rel or []:
                session.delete(s)
            session.flush()
        # Rebuild subtasks from list
        for s in state.get("subtasks", []):
            sr = Subtask(
                task_id=task_id,
                subtask_id=s.get("subtask_id") or s.get("id"),
                name=s.get("name"),
                status=s.get("status", "pending"),
                retry_count=int(s.get("retry_count", 0) or 0),
                criteria_ref=s.get("criteria_ref"),
                evidence_summary=s.get("evidence_summary"),
                latest_state=s.get("latest_state", {}) or {},
                completion_criteria=s.get("completion_criteria", {}) or {},
                user_overrides=s.get("user_overrides", {}) or {},
                parameters=s.get("parameters", {}) or {},
                anomalies_config=s.get("anomalies", {}) or {},
            )
            sr.task = task_pk
            session.add(sr)

    @staticmethod
    def _update_task_row(row: Task, state: Dict[str, Any]) -> None:
        row.description = state.get("description", row.description)
        row.overall_status = state.get("overall_status", row.overall_status)
        row.current_subtask = state.get("current_subtask")
        meta = state.get("metadata", {}) or {}
        row.priority = int(meta.get("priority", row.priority or 0))
        row.task_type = meta.get("task_type", row.task_type)
        row.created_at = float(state.get("created_at", row.created_at or 0) or 0)
        row.updated_at = time.time()
        row.metadata = meta
        row.global_parameters = state.get("global_parameters", {}) or {}
        row.anomaly_state = state.get("anomaly_state", {}) or {}
        row.latest_anomaly_context = state.get("latest_anomaly_context")
        row.latest_anomaly_advice = state.get("latest_anomaly_advice")
        row.notifications = state.get("notifications", []) or []
        row.pending_intervention = state.get("pending_intervention")
