"""Deterministic extraction of task admission facts from task state."""

from typing import Any, Dict, Iterable, List


STATIC_QUERY_TOPICS = {
    "task_identity",
    "time",
    "location",
    "task_details",
    "equipment",
    "conditions",
}


def resolve_task_intent(task_state: Dict[str, Any]) -> Dict[str, Any]:
    """Return the original TaskIntent from either supported metadata shape."""
    metadata = (task_state or {}).get("metadata") or {}
    if not isinstance(metadata, dict):
        return {}
    intent = metadata.get("intent")
    if isinstance(intent, dict):
        return intent
    return metadata


def available_task_intent_sections(task_state: Dict[str, Any]) -> List[str]:
    intent = resolve_task_intent(task_state)
    sections: List[str] = []
    if any(intent.get(key) is not None for key in ("intent_id", "task_type", "priority")):
        sections.append("task_identity")
    if isinstance(intent.get("time"), dict) and intent.get("time"):
        sections.append("time")
    if isinstance(intent.get("location"), dict) and intent.get("location"):
        sections.append("location")
    details = ((intent.get("task") or {}).get("details") if isinstance(intent.get("task"), dict) else None)
    if isinstance(details, dict) and details:
        sections.append("task_details")
    if isinstance(intent.get("equipment"), dict) and intent.get("equipment"):
        sections.append("equipment")
    if isinstance(intent.get("conditions"), dict) and intent.get("conditions"):
        sections.append("conditions")
    return sections


def compact_task_intent_summary(task_state: Dict[str, Any]) -> Dict[str, Any]:
    """Build a small routing summary without exposing runtime examples."""
    intent = resolve_task_intent(task_state)
    if not intent:
        return {}

    summary: Dict[str, Any] = {
        "intent_id": intent.get("intent_id"),
        "task_type": intent.get("task_type"),
        "priority": intent.get("priority"),
        "available_sections": available_task_intent_sections(task_state),
    }
    location = intent.get("location")
    if isinstance(location, dict):
        summary["location"] = {
            "oilfield": location.get("oilfield"),
            "water_depth_m": location.get("water_depth_m"),
        }
    equipment = intent.get("equipment")
    if isinstance(equipment, dict):
        summary["equipment"] = {
            "robot_type": equipment.get("robot_type"),
        }
    details = ((intent.get("task") or {}).get("details") if isinstance(intent.get("task"), dict) else None)
    if isinstance(details, dict):
        summary["task_details"] = {
            "wellhead_id": details.get("wellhead_id"),
            "target_latitude": details.get("target_latitude"),
            "target_longitude": details.get("target_longitude"),
            "xmas_tree_type": details.get("xmas_tree_type"),
            "hole_id": details.get("hole_id"),
        }
    return _drop_empty(summary)


def retrieve_task_intent(task_state: Dict[str, Any], query_topics: Iterable[str]) -> Dict[str, Any]:
    """Extract only the requested static TaskIntent fields."""
    intent = resolve_task_intent(task_state)
    topics = set(query_topics or [])
    facts: Dict[str, Any] = {}

    if "task_identity" in topics:
        facts["任务身份"] = _drop_empty({
            "任务编号": intent.get("intent_id"),
            "任务类型": intent.get("task_type"),
            "优先级": intent.get("priority"),
        })
    if "time" in topics:
        facts["任务时间"] = intent.get("time") or {}
    if "location" in topics:
        facts["任务位置"] = intent.get("location") or {}
    if "task_details" in topics:
        task = intent.get("task") or {}
        facts["任务目标"] = (task.get("details") if isinstance(task, dict) else {}) or {}
    if "equipment" in topics:
        facts["作业设备"] = intent.get("equipment") or {}
    if "conditions" in topics:
        facts["作业条件"] = intent.get("conditions") or {}

    return {key: value for key, value in facts.items() if value}


def _drop_empty(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != {} and item != []
    }
