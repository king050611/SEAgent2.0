"""Context assembly helpers for anomaly advice.

The advisor uses backend-provided ``anomaly_state`` as the primary anomaly
source. Legacy diagnostic signals from ``latest_state`` are still normalized and
kept in the context for compatibility and supporting evidence, but they should
not be used as the main anomaly classification source in the new advisor flow.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Dict, Optional


STATUS_NORMAL = "normal"
STATUS_ABNORMAL = "abnormal"
STATUS_UNKNOWN = "unknown"


def _normalize_status(value: Any) -> str:
    """Normalize booleans, 0/1 flags, and status strings."""
    if value is None:
        return STATUS_UNKNOWN
    if isinstance(value, bool):
        return STATUS_NORMAL if value else STATUS_ABNORMAL
    if isinstance(value, (int, float)):
        return STATUS_NORMAL if value == 1 else STATUS_ABNORMAL if value == 0 else STATUS_UNKNOWN
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "ok", "normal", "success", "valid"}:
            return STATUS_NORMAL
        if text in {"0", "false", "fail", "failed", "abnormal", "error", "invalid", "timeout"}:
            return STATUS_ABNORMAL
        if text in {"unknown", "none", "null", ""}:
            return STATUS_UNKNOWN
    return STATUS_UNKNOWN


def _normalize_evidence(evidence: Any) -> list[str]:
    if evidence is None:
        return []
    if isinstance(evidence, list):
        return [str(item) for item in evidence if item is not None]
    return [str(evidence)]


def normalize_anomaly_state(raw_state: Any) -> Dict[str, Dict[str, Any]]:
    """Normalize backend-provided anomaly_state without inferring categories.

    Supports both compact and detailed forms:
    - execution: "abnormal"
    - execution: {status: "abnormal", severity: "high", evidence: [...]}
    """
    if not isinstance(raw_state, dict):
        return {}

    normalized: Dict[str, Dict[str, Any]] = {}
    for key, value in raw_state.items():
        if isinstance(value, dict):
            normalized[key] = {
                "status": _normalize_status(value.get("status")),
                "severity": value.get("severity"),
                "evidence": _normalize_evidence(value.get("evidence")),
                "raw": copy.deepcopy(value),
            }
        else:
            normalized[key] = {
                "status": _normalize_status(value),
                "severity": None,
                "evidence": [],
                "raw": copy.deepcopy(value),
            }
    return normalized


def _signal_status(signals: Dict[str, Any], key: str) -> str:
    raw = signals.get(key)
    if isinstance(raw, dict):
        return _normalize_status(raw.get("status"))
    return _normalize_status(raw)


def normalize_diagnostic_signals(latest_state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return normalized diagnostic signals without modifying latest_state.

    Supports both forms:
    - flat flags: data_comm=1, planning=0, arm_state_flag=1
    - nested: diagnostic_signals.data_comm.status = "normal"
    """
    latest_state = latest_state or {}
    nested = latest_state.get("diagnostic_signals") or {}
    if not isinstance(nested, dict):
        nested = {}

    path_value = latest_state.get("planning", latest_state.get("path_planing"))

    signal_sources = {
        "data_comm": latest_state.get("data_comm"),
        "planning": path_value,
        "execution": latest_state.get("execution_status"),
        "plant": latest_state.get("plant_status"),
        "perception": latest_state.get("perception_status"),
        "arm_state": latest_state.get("arm_state_flag"),
        "robotic_power": latest_state.get("robotic_power_flag", latest_state.get("robitic_power_flag")),
        "operation": latest_state.get("operation_flag"),
        "visual_state": latest_state.get("visual_state_flag"),
        "visual_gps": latest_state.get("visual_gps_flag"),
    }

    normalized: Dict[str, Dict[str, Any]] = {}
    for key, flat_value in signal_sources.items():
        nested_status = _signal_status(nested, key)
        flat_status = _normalize_status(flat_value)
        status = nested_status if nested_status != STATUS_UNKNOWN else flat_status
        normalized[key] = {"status": status}

    return normalized


def get_current_subtask(task_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    current_id = (task_state or {}).get("current_subtask")
    for subtask in (task_state or {}).get("subtasks", []):
        if subtask.get("subtask_id") == current_id:
            return subtask
    return None


def _get_raw_anomaly_state(
    task_state: Dict[str, Any],
    subtask: Dict[str, Any],
    latest_state: Dict[str, Any],
) -> Any:
    if isinstance(task_state.get("anomaly_state"), dict):
        return task_state.get("anomaly_state")
    if isinstance(subtask.get("anomaly_state"), dict):
        return subtask.get("anomaly_state")
    if isinstance(latest_state.get("anomaly_state"), dict):
        return latest_state.get("anomaly_state")
    return {}


def build_anomaly_context(
    task_state: Dict[str, Any],
    subtask: Dict[str, Any],
    failed_criteria: Optional[list[str]] = None,
    anomaly_key: Optional[str] = None,
    system_action: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a self-contained anomaly context from task and subtask state."""
    task_state = task_state or {}
    subtask = subtask or {}
    latest_state = copy.deepcopy(subtask.get("latest_state") or {})
    completion_criteria = copy.deepcopy(subtask.get("completion_criteria") or {})
    failed = failed_criteria
    if failed is None:
        failed = completion_criteria.get("hard_unmet_details", [])

    raw_anomaly_state = copy.deepcopy(_get_raw_anomaly_state(task_state, subtask, latest_state))

    return {
        "task_id": task_state.get("task_id"),
        "description": task_state.get("description", ""),
        "overall_status": task_state.get("overall_status"),
        "current_subtask": {
            "subtask_id": subtask.get("subtask_id"),
            "name": subtask.get("name", subtask.get("subtask_id")),
            "status": subtask.get("status"),
            "retry_count": subtask.get("retry_count", 0),
            "max_retries": subtask.get("max_retries", 0),
        },
        "latest_state": latest_state,
        "anomaly_state": normalize_anomaly_state(raw_anomaly_state),
        "raw_anomaly_state": raw_anomaly_state,
        "diagnostic_signals": normalize_diagnostic_signals(latest_state),
        "completion_criteria": completion_criteria,
        "failed_criteria": list(failed or []),
        "anomaly_key": anomaly_key,
        "system_action": copy.deepcopy(system_action),
        "created_at": time.time(),
    }


def get_latest_anomaly_context(task_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    context = (task_state or {}).get("latest_anomaly_context")
    if isinstance(context, dict):
        return copy.deepcopy(context)
    return None
