"""
db/models.py – SQLAlchemy ORM models for the upgraded PostgreSQL + JSONB
hybrid schema. Keeps the same logical shape as the original JSON file
storage, but indexes the hot columns and stashes flexible data in JSONB.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, List

from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    BigInteger,
    Float,
    Boolean,
    Index,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

Base = declarative_base()


class Task(Base):
    __tablename__ = "tasks"

    task_id = Column(String(64), primary_key=True)
    description = Column(Text, nullable=False)
    overall_status = Column(String(32), nullable=False, default="in_progress")
    current_subtask = Column(String(64))
    priority = Column(Integer, default=0)
    task_type = Column(String(64))
    created_at = Column(Float, nullable=False, server_default=func.extract("epoch", func.now()))
    updated_at = Column(Float, nullable=False, server_default=func.extract("epoch", func.now()))

    metadata = Column(JSONB, nullable=False, server_default="{}")
    global_parameters = Column(JSONB, nullable=False, server_default="{}")
    anomaly_state = Column(JSONB, nullable=False, server_default="{}")
    latest_anomaly_context = Column(JSONB, server_default="{}")
    latest_anomaly_advice = Column(JSONB, server_default="{}")
    notifications = Column(JSONB, nullable=False, server_default="[]")
    pending_intervention = Column(JSONB, server_default="{}")

    subtasks_rel = relationship(
        "Subtask",
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("idx_tasks_status_created", "overall_status", "created_at", postgresql_ops={"created_at": "DESC NULLS LAST"}),
        Index("idx_tasks_metadata_gin", "metadata", postgresql_using="gin", postgresql_ops={"metadata": "jsonb_path_ops"}),
    )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "task_id": self.task_id,
            "description": self.description,
            "created_at": self.created_at,
            "overall_status": self.overall_status,
            "current_subtask": self.current_subtask,
            "metadata": dict(self.metadata or {}),
            "global_parameters": dict(self.global_parameters or {}),
            "anomaly_state": dict(self.anomaly_state or {}),
            "latest_anomaly_context": self.latest_anomaly_context,
            "latest_anomaly_advice": self.latest_anomaly_advice,
            "notifications": list(self.notifications or []),
            "pending_intervention": self.pending_intervention or None,
            "subtasks": [s.to_dict() for s in sorted(self.subtasks_rel or [], key=lambda x: x.id)],
        }
        return d

    @staticmethod
    def from_dict(state: Dict[str, Any]) -> "Task":
        t = Task(
            task_id=state["task_id"],
            description=state.get("description", ""),
            overall_status=state.get("overall_status", "in_progress"),
            current_subtask=state.get("current_subtask"),
            priority=int((state.get("metadata") or {}).get("priority", 0) or 0),
            task_type=(state.get("metadata") or {}).get("task_type"),
            created_at=float(state.get("created_at", 0) or 0),
            updated_at=float(state.get("updated_at", 0) or 0),
            metadata=state.get("metadata", {}) or {},
            global_parameters=state.get("global_parameters", {}) or {},
            anomaly_state=state.get("anomaly_state", {}) or {},
            latest_anomaly_context=state.get("latest_anomaly_context"),
            latest_anomaly_advice=state.get("latest_anomaly_advice"),
            notifications=state.get("notifications", []) or [],
            pending_intervention=state.get("pending_intervention"),
        )
        return t


class Subtask(Base):
    __tablename__ = "subtasks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(String(64), ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False)
    subtask_id = Column(String(64), nullable=False)
    name = Column(String(255))
    status = Column(String(32), nullable=False, default="pending")
    retry_count = Column(Integer, nullable=False, default=0)
    criteria_ref = Column(String(128))
    evidence_summary = Column(Text)
    latest_state = Column(JSONB, nullable=False, server_default="{}")
    completion_criteria = Column(JSONB, nullable=False, server_default="{}")
    user_overrides = Column(JSONB, nullable=False, server_default="{}")
    parameters = Column(JSONB, nullable=False, server_default="{}")
    anomalies_config = Column(JSONB, nullable=False, server_default="{}")
    created_at = Column(Float, nullable=False, server_default=func.extract("epoch", func.now()))
    updated_at = Column(Float, nullable=False, server_default=func.extract("epoch", func.now()))

    task = relationship("Task", back_populates="subtasks_rel")

    __table_args__ = (
        UniqueConstraint("task_id", "subtask_id", name="uq_subtasks_task_subtask"),
        Index("idx_subtasks_task_status", "task_id", "status"),
        Index("idx_subtasks_criteria_ref", "criteria_ref"),
        Index("idx_subtasks_latest_state_gin", "latest_state", postgresql_using="gin", postgresql_ops={"latest_state": "jsonb_path_ops"}),
        Index("idx_subtasks_user_overrides_gin", "user_overrides", postgresql_using="gin", postgresql_ops={"user_overrides": "jsonb_path_ops"}),
        Index(
            "idx_subtasks_latest_distance_err",
            func.cast(func.jsonb_extract_path_text("latest_state", "distance_error_m"), Float),
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "id": self.subtask_id,  # backward compat key
            "name": self.name,
            "status": self.status,
            "retry_count": self.retry_count or 0,
            "criteria_ref": self.criteria_ref,
            "evidence_summary": self.evidence_summary or "",
            "latest_state": dict(self.latest_state or {}),
            "completion_criteria": dict(self.completion_criteria or {}),
            "user_overrides": dict(self.user_overrides or {}),
            "parameters": dict(self.parameters or {}),
            "anomalies": dict(self.anomalies_config or {}),
        }

    def apply_from_dict(self, d: Dict[str, Any]) -> None:
        self.name = d.get("name", self.name)
        self.status = d.get("status", self.status)
        self.retry_count = int(d.get("retry_count", self.retry_count or 0))
        self.criteria_ref = d.get("criteria_ref", self.criteria_ref)
        self.evidence_summary = d.get("evidence_summary", self.evidence_summary)
        self.latest_state = d.get("latest_state", self.latest_state or {}) or {}
        self.completion_criteria = d.get("completion_criteria", self.completion_criteria or {}) or {}
        self.user_overrides = d.get("user_overrides", self.user_overrides or {}) or {}
        self.parameters = d.get("parameters", self.parameters or {}) or {}
        self.anomalies_config = d.get("anomalies", self.anomalies_config or {}) or {}
        self.updated_at = float(func.extract("epoch", func.now()).compile(compile_kwargs={"literal_binds": True})) if False else __import__("time").time()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(String(64), index=True)
    subtask_id = Column(String(64))
    user_id = Column(String(128), default="anonymous")
    action = Column(String(64), nullable=False)
    action_category = Column(String(32), nullable=False)
    severity = Column(String(16), nullable=False, default="info")
    input_message = Column(Text)
    action_payload = Column(JSONB)
    before_state = Column(JSONB)
    after_state = Column(JSONB)
    decision_path = Column(String(255))
    llm_confidence = Column(Float)
    result_ok = Column(Boolean)
    error_message = Column(Text)
    created_at = Column(Float, nullable=False, server_default=func.extract("epoch", func.now()))

    __table_args__ = (
        Index("idx_audit_task_time", "task_id", "created_at", postgresql_ops={"created_at": "DESC NULLS LAST"}),
        Index("idx_audit_action_cat", "action_category", "created_at", postgresql_ops={"created_at": "DESC NULLS LAST"}),
        Index("idx_audit_severity", "severity", "created_at", postgresql_ops={"created_at": "DESC NULLS LAST"}),
    )


class CacheMeta(Base):
    __tablename__ = "cache_meta"

    cache_layer = Column(String(16), primary_key=True)
    hits = Column(BigInteger, nullable=False, default=0)
    misses = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(Float, nullable=False, server_default=func.extract("epoch", func.now()))
