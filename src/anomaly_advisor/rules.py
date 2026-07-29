"""Rule matching for the anomaly advice system.

Runtime anomaly status is provided by the backend through ``anomaly_state``.
This matcher intersects abnormal states with the current subtask profile before
building user-facing diagnosis context. Filtered anomalies remain available for
internal traceability, but should not be exposed to the reply model by default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


STATUS_ABNORMAL = "abnormal"
STATUS_NORMAL = "normal"
STATUS_UNKNOWN = "unknown"


class AnomalyRuleMatcher:
    """Match backend-provided anomaly_state against current subtask profiles."""

    def __init__(
        self,
        advice_config: Optional[Dict[str, Any]] = None,
        config_path: Optional[str | Path] = None,
    ):
        self.config = advice_config or self._load_config(config_path)
        self.component_groups = self.config.get("component_groups") or {}
        self.anomaly_types = self.config.get("anomaly_types") or {}
        self.task_profiles = self.config.get("task_anomaly_profiles") or {}
        self.default_advice = self.config.get("default_advice") or {}

    def classify(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Return a scoped anomaly diagnosis context for abnormal states."""
        subtask_id = self._subtask_id(context)
        anomaly_state = self._normalize_anomaly_state(context.get("anomaly_state"))
        profile = self._task_profile(subtask_id)
        possible_map = self._possible_anomaly_map(profile)
        enforce_profile_scope = bool(self.task_profiles)
        filtered = self._empty_filtered()

        if not anomaly_state:
            return self._no_advice_result(
                subtask_id=subtask_id,
                matched_rule="no_anomaly_state",
                summary=self.default_advice.get(
                    "missing_anomaly_state",
                    "当前任务没有可用的 anomaly_state，请先补充后端异常状态。",
                ),
                filtered_anomalies=filtered,
                task_profile=profile,
            )

        matched: List[Dict[str, Any]] = []
        for anomaly_key, state_info in self._state_items(anomaly_state):
            status = (state_info or {}).get("status")
            if status == STATUS_NORMAL:
                filtered["normal"].append(self._filter_record(anomaly_key, state_info, "状态为 normal，常规异常回复不展开。"))
                continue
            if status == STATUS_UNKNOWN:
                filtered["unknown"].append(self._filter_record(anomaly_key, state_info, "状态为 unknown，证据不足，常规异常回复不展开。"))
                continue
            if status != STATUS_ABNORMAL:
                filtered["unknown"].append(self._filter_record(anomaly_key, state_info, "状态无法识别，常规异常回复不展开。"))
                continue

            type_def = self.anomaly_types.get(anomaly_key)
            if not type_def:
                filtered["unsupported"].append(self._filter_record(anomaly_key, state_info, "异常类型不在通用 anomaly_types 中。"))
                continue
            if enforce_profile_scope and anomaly_key not in possible_map:
                filtered["out_of_scope"].append(
                    self._filter_record(
                        anomaly_key,
                        state_info,
                        f"该异常未出现在当前子任务 {subtask_id} 的 possible_anomalies 中。",
                    )
                )
                continue
            profile_item = possible_map.get(anomaly_key, {})
            matched.append(self._build_match(subtask_id, anomaly_key, state_info, type_def, profile_item))

        if not matched:
            any_abnormal = any(
                (item or {}).get("status") == STATUS_ABNORMAL for item in anomaly_state.values()
            )
            summary_key = "no_supported_abnormal_anomaly" if any_abnormal else "no_abnormal_anomaly"
            if filtered.get("out_of_scope") and not any(filtered.get(key) for key in ("unsupported",)):
                summary_key = "no_supported_abnormal_anomaly"
            return self._no_advice_result(
                subtask_id=subtask_id,
                matched_rule=summary_key,
                summary=self.default_advice.get(
                    summary_key,
                    "当前没有匹配到可输出的异常诊断上下文。",
                ),
                filtered_anomalies=filtered,
                task_profile=profile,
            )

        matched.sort(key=lambda item: item.get("priority", 999))
        primary = matched[0]
        candidate_component_groups = self._unique_candidate_component_groups(matched)
        reasoning_hints = self._unique_reasoning_hints(matched)
        return {
            "advice_generated": True,
            "exception_category": primary["exception_category"],
            "anomaly_type": primary["anomaly_type"],
            "confidence": primary.get("confidence", "medium"),
            "dependency_level": primary.get("priority"),
            "stage_meaning": primary.get("stage_meaning"),
            "summary": primary.get("stage_meaning"),
            "evidence_used": primary.get("evidence_used", []),
            "missing_evidence": [],
            "matched_rule": primary.get("matched_rule"),
            "primary_suggestion": primary.get("primary_suggestion"),
            "secondary_suggestions": primary.get("secondary_suggestions", []),
            "fallback_only_if_not_resolved": [],
            "candidate_categories": [item["exception_category"] for item in matched],
            "matched_anomalies": matched,
            "filtered_anomalies": filtered,
            "component_groups": [],
            "candidate_component_groups": candidate_component_groups,
            "task_profile": self._compact_task_profile(profile),
            "scope_rule": "仅解释 matched_anomalies；filtered_anomalies 只用于内部追溯，除非用户明确询问过滤原因或原始异常状态，否则不要展开。",
            "anomaly_context": {
                "matched_anomaly_types": [item["anomaly_type"] for item in matched],
                "candidate_component_groups": candidate_component_groups,
                "reasoning_hints": reasoning_hints,
                "task_profile": self._compact_task_profile(profile),
                "scope_rule": "matched_anomalies 是当前 abnormal 且属于当前子任务 possible_anomalies 的交集。",
            },
        }

    def _load_config(self, config_path: Optional[str | Path]) -> Dict[str, Any]:
        path = Path(config_path) if config_path else Path(__file__).resolve().parents[2] / "config" / "anomaly_advice.yaml"
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            return {}
        return data

    def _subtask_id(self, context: Dict[str, Any]) -> str:
        return ((context.get("current_subtask") or {}).get("subtask_id") or "").upper()

    def _task_profile(self, subtask_id: str) -> Dict[str, Any]:
        profile = self.task_profiles.get(subtask_id) or self.task_profiles.get(str(subtask_id).upper()) or {}
        return profile if isinstance(profile, dict) else {}

    def _possible_anomaly_map(self, profile: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        possible: Dict[str, Dict[str, Any]] = {}
        for item in profile.get("possible_anomalies") or []:
            if not isinstance(item, dict):
                continue
            key = item.get("type")
            if key:
                possible[str(key)] = item
        return possible

    def _normalize_anomaly_state(self, raw_state: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(raw_state, dict):
            return {}

        normalized: Dict[str, Dict[str, Any]] = {}
        for key, value in raw_state.items():
            if isinstance(value, dict):
                status = self._normalize_status(value.get("status"))
                normalized[key] = {
                    "status": status,
                    "severity": value.get("severity"),
                    "evidence": self._normalize_evidence(value.get("evidence")),
                    "raw": value,
                }
            else:
                normalized[key] = {
                    "status": self._normalize_status(value),
                    "severity": None,
                    "evidence": [],
                    "raw": value,
                }
        return normalized

    def _normalize_status(self, value: Any) -> str:
        if value is None:
            return STATUS_UNKNOWN
        if isinstance(value, bool):
            return STATUS_NORMAL if value else STATUS_ABNORMAL
        if isinstance(value, (int, float)):
            if value == 1:
                return STATUS_NORMAL
            if value == 0:
                return STATUS_ABNORMAL
            return STATUS_UNKNOWN
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "ok", "normal", "success", "valid"}:
                return STATUS_NORMAL
            if text in {"0", "false", "fail", "failed", "abnormal", "error", "invalid", "timeout"}:
                return STATUS_ABNORMAL
            if text in {"unknown", "none", "null", ""}:
                return STATUS_UNKNOWN
        return STATUS_UNKNOWN

    def _normalize_evidence(self, evidence: Any) -> List[str]:
        if evidence is None:
            return []
        if isinstance(evidence, list):
            return [str(item) for item in evidence if item is not None]
        return [str(evidence)]

    def _state_items(self, anomaly_state: Dict[str, Dict[str, Any]]) -> List[tuple[str, Dict[str, Any]]]:
        return sorted(anomaly_state.items(), key=lambda item: self._priority(item[0]))

    def _priority(self, anomaly_key: str) -> int:
        type_def = self.anomaly_types.get(anomaly_key) or {}
        priority = type_def.get("priority", 999)
        try:
            return int(priority)
        except (TypeError, ValueError):
            return 999

    def _component_group_details(self, group_keys: List[str]) -> List[Dict[str, Any]]:
        details: List[Dict[str, Any]] = []
        for key in group_keys:
            group = self.component_groups.get(key) or {}
            details.append(
                {
                    "key": key,
                    "name": group.get("name", key),
                    "meaning": group.get("meaning", ""),
                    "includes": list(group.get("includes") or []),
                }
            )
        return details

    def _build_match(
        self,
        subtask_id: str,
        anomaly_key: str,
        state_info: Dict[str, Any],
        type_def: Dict[str, Any],
        profile_item: Dict[str, Any],
    ) -> Dict[str, Any]:
        evidence = list(state_info.get("evidence") or [])
        evidence.insert(0, f"anomaly_state.{anomaly_key}=abnormal")
        group_keys = list(type_def.get("candidate_component_groups") or [])
        category = type_def.get("name", anomaly_key)
        meaning = type_def.get("meaning", "")
        role = profile_item.get("role") or ""
        relevance = profile_item.get("relevance")
        selection_rule = type_def.get("component_selection_rule", "")
        return {
            "anomaly_type": anomaly_key,
            "exception_category": category,
            "priority": self._priority(anomaly_key),
            "action_type": type_def.get("action_type"),
            "confidence": "medium",
            "severity": state_info.get("severity"),
            "stage_meaning": self._stage_meaning(category, meaning, role),
            "meaning": meaning,
            "task_profile_role": role,
            "task_profile_relevance": relevance,
            "candidate_component_groups": group_keys,
            "candidate_component_group_details": self._component_group_details(group_keys),
            "component_selection_rule": selection_rule,
            "reasoning_hints": list(type_def.get("reasoning_hints") or []),
            "evidence_used": evidence,
            "missing_evidence": [],
            "matched_rule": f"{subtask_id}.{anomaly_key}" if subtask_id else f"generic.{anomaly_key}",
            "primary_suggestion": {
                "action_type": type_def.get("action_type"),
                "text": self._generic_suggestion_text(category, group_keys, role),
            },
            "secondary_suggestions": list(type_def.get("reasoning_hints") or []),
            "fallback_only_if_not_resolved": [],
            "subtask_id": subtask_id,
        }

    def _stage_meaning(self, category: str, meaning: str, role: str) -> str:
        if role:
            return f"{category}在当前子任务中主要可能{role}"
        return meaning

    def _generic_suggestion_text(self, category: str, group_keys: List[str], role: str) -> str:
        group_names = [
            (self.component_groups.get(key) or {}).get("name", key)
            for key in group_keys
        ]
        role_part = f"，重点关注其是否{role}" if role else ""
        if group_names:
            return f"结合当前子任务职责和未满足判据，先从候选系统（{'、'.join(group_names)}）中裁剪出直接相关对象{role_part}，不要平均展开全部候选系统。"
        return f"结合当前子任务和未满足判据，保守分析可能相关的{category}{role_part}。"

    def _filter_record(self, anomaly_key: str, state_info: Dict[str, Any], reason: str) -> Dict[str, Any]:
        type_def = self.anomaly_types.get(anomaly_key) or {}
        return {
            "type": anomaly_key,
            "category": type_def.get("name", anomaly_key),
            "status": (state_info or {}).get("status"),
            "severity": (state_info or {}).get("severity"),
            "reason": reason,
        }

    def _empty_filtered(self) -> Dict[str, List[Dict[str, Any]]]:
        return {"normal": [], "unknown": [], "out_of_scope": [], "unsupported": []}

    def _compact_task_profile(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        if not profile:
            return {}
        return {
            "name": profile.get("name"),
            "goal": profile.get("goal"),
            "possible_anomalies": [
                {
                    "type": item.get("type"),
                    "relevance": item.get("relevance"),
                    "role": item.get("role"),
                }
                for item in profile.get("possible_anomalies") or []
                if isinstance(item, dict)
            ],
        }

    def _unique_candidate_component_groups(self, matched: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen: set[str] = set()
        groups: List[Dict[str, Any]] = []
        for item in matched:
            for group in item.get("candidate_component_group_details") or []:
                key = group.get("key")
                if not key or key in seen:
                    continue
                seen.add(key)
                groups.append(group)
        return groups

    def _unique_reasoning_hints(self, matched: List[Dict[str, Any]]) -> List[str]:
        seen: set[str] = set()
        hints: List[str] = []
        for item in matched:
            for hint in item.get("reasoning_hints") or []:
                text = str(hint)
                if not text or text in seen:
                    continue
                seen.add(text)
                hints.append(text)
        return hints

    def _no_advice_result(
        self,
        subtask_id: str,
        matched_rule: str,
        summary: str,
        filtered_anomalies: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        task_profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        filtered = filtered_anomalies or self._empty_filtered()
        return {
            "advice_generated": False,
            "exception_category": "暂无可用异常诊断上下文",
            "anomaly_type": None,
            "confidence": "low",
            "dependency_level": None,
            "stage_meaning": "",
            "summary": summary,
            "evidence_used": [],
            "missing_evidence": [],
            "matched_rule": matched_rule,
            "primary_suggestion": {
                "action_type": "no_advice",
                "text": summary,
            },
            "secondary_suggestions": [],
            "fallback_only_if_not_resolved": [],
            "candidate_categories": [],
            "matched_anomalies": [],
            "filtered_anomalies": filtered,
            "component_groups": [],
            "candidate_component_groups": [],
            "task_profile": self._compact_task_profile(task_profile or {}),
            "scope_rule": "仅解释 matched_anomalies；filtered_anomalies 只用于内部追溯，除非用户明确询问过滤原因或原始异常状态，否则不要展开。",
            "anomaly_context": {
                "matched_anomaly_types": [],
                "candidate_component_groups": [],
                "reasoning_hints": [],
                "task_profile": self._compact_task_profile(task_profile or {}),
                "scope_rule": "没有当前子任务范围内可输出的异常。",
            },
            "subtask_id": subtask_id,
        }
