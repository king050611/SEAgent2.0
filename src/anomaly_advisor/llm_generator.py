"""Advice generation for backend-provided anomaly_state.

MVP behavior uses deterministic fallback text. If an llm_client with
``extract_json(messages)`` is provided, the class will try LLM generation first
and fall back to templates on failure.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class AnomalyAdviceGenerator:
    def __init__(self, llm_client: Optional[Any] = None):
        self.llm_client = llm_client

    def generate(
        self,
        context: Dict[str, Any],
        classification: Dict[str, Any],
        user_question: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.llm_client and classification.get("advice_generated"):
            llm_result = self._generate_with_llm(context, classification, user_question)
            if llm_result:
                return llm_result
        return self._generate_fallback(context, classification)

    def _generate_with_llm(
        self,
        context: Dict[str, Any],
        classification: Dict[str, Any],
        user_question: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        messages = self._build_prompt(context, classification, user_question)
        try:
            result = self.llm_client.extract_json(messages, max_tokens=1600)
        except Exception:
            return None
        if not isinstance(result, dict):
            return None
        result.setdefault("advice_generated", classification.get("advice_generated", True))
        result["adoption_flag"] = False
        result.setdefault("exception_category", classification.get("exception_category"))
        result.setdefault("anomaly_type", classification.get("anomaly_type"))
        result.setdefault("stage_meaning", classification.get("stage_meaning"))
        result.setdefault("primary_suggestion", classification.get("primary_suggestion"))
        result.setdefault("secondary_suggestions", classification.get("secondary_suggestions", []))
        result.setdefault("fallback_only_if_not_resolved", classification.get("fallback_only_if_not_resolved", []))
        result.setdefault("evidence_used", classification.get("evidence_used", []))
        result.setdefault("missing_evidence", classification.get("missing_evidence", []))
        result.setdefault("matched_anomalies", classification.get("matched_anomalies", []))
        result.setdefault("anomaly_context", classification.get("anomaly_context", {}))
        result.setdefault("component_groups", classification.get("component_groups", []))
        result.setdefault("candidate_component_groups", classification.get("candidate_component_groups", []))
        result.setdefault("task_profile", classification.get("task_profile", {}))
        result.setdefault("scope_rule", classification.get("scope_rule"))
        result.setdefault("filtered_anomalies", classification.get("filtered_anomalies", {}))
        result.setdefault("system_action_note", self._system_action_note(context))
        result["failure_observation"] = self._normalize_failure_observation(
            result.get("failure_observation"),
            context,
        )
        result["anomaly_evidence"] = self._normalize_anomaly_evidence(
            result.get("anomaly_evidence"),
            classification,
        )
        result["selected_fault_modules"] = self._normalize_selected_fault_modules(
            result.get("selected_fault_modules"),
            context,
            classification,
        )
        result.setdefault("advice_text", self._render_text(result))
        return result

    def _build_prompt(
        self,
        context: Dict[str, Any],
        classification: Dict[str, Any],
        user_question: Optional[str],
    ) -> list[dict]:
        subtask = context.get("current_subtask") or {}
        return [
            {
                "role": "system",
                "content": (
                    "你是ROV任务异常建议模块。必须遵守："
                    "1) anomaly_state 已由后端判定，不得重新分类；"
                    "2) 只说明并解释 classification 中已匹配到的异常；"
                    "3) 不输出未支持或未配置异常的内部原因；"
                    "4) 先给当前阶段处理建议，不要直接建议回退；"
                    "5) 回退只能放在 fallback_only_if_not_resolved；"
                    "6) adoption_flag 必须为 false；"
                    "7) 必须区分 failure_observation 与 anomaly_evidence：failure_observation 只描述子任务失败现象和未满足判据，anomaly_evidence 只描述机器人或环境侧异常证据；"
                    "8) 失败不等于异常。没有 abnormal anomaly_state 或 matched_anomalies 时，不得把判据失败写成已发生内部异常；"
                    "9) 必须从 classification.candidate_component_groups 中选择与当前子任务、失败判据、anomaly_state 和 matched_anomalies 最直接相关的故障模块，输出 selected_fault_modules；"
                    "10) 不得创造候选范围外的模块，证据不足时给出保守判断并降低 confidence；"
                    "11) advice_text 面向现场人员自然表达，不得出现 anomaly_advisor、classification、matched_anomalies、selected_fault_modules、规则匹配、模块匹配等内部术语。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户问题：{user_question or '当前有什么异常，应该如何解决？'}\n"
                    f"任务：{context.get('description', '')}\n"
                    f"当前子任务：{subtask.get('subtask_id')} {subtask.get('name')}\n"
                    f"子任务状态：{subtask.get('status')}\n"
                    f"任务级 anomaly_state：{context.get('anomaly_state')}\n"
                    f"失败判据：{context.get('failed_criteria')}\n"
                    f"匹配结果：{classification}\n"
                    "请输出 JSON，字段包括 advice_generated, adoption_flag, task_status_summary, "
                    "exception_category, anomaly_type, confidence, summary, stage_meaning, "
                    "evidence_used, missing_evidence, primary_suggestion, secondary_suggestions, "
                    "fallback_only_if_not_resolved, matched_anomalies, anomaly_context, component_groups, "
                    "candidate_component_groups, selected_fault_modules, advice_text。\n"
                    "selected_fault_modules 的每个元素必须包含 module_key, module_name, "
                    "related_anomaly_type, confidence, reason, evidence, suggested_checks。"
                ),
            },
        ]

    def _generate_fallback(self, context: Dict[str, Any], classification: Dict[str, Any]) -> Dict[str, Any]:
        subtask = context.get("current_subtask") or {}
        matched = classification.get("matched_anomalies") or []
        primary = classification.get("primary_suggestion") or {}
        fallback = classification.get("fallback_only_if_not_resolved", [])
        evidence = classification.get("evidence_used", [])
        missing = classification.get("missing_evidence", [])
        generated = bool(classification.get("advice_generated"))

        result = {
            "advice_generated": generated,
            "adoption_flag": False,
            "task_status_summary": self._task_status_summary(subtask),
            "exception_category": classification.get("exception_category", "暂无可用异常建议"),
            "anomaly_type": classification.get("anomaly_type"),
            "dependency_level": classification.get("dependency_level"),
            "confidence": classification.get("confidence", "medium" if generated else "low"),
            "summary": self._summary_text(subtask, classification),
            "stage_meaning": classification.get("stage_meaning", ""),
            "cause_analysis": self._cause_analysis(classification),
            "evidence_used": evidence,
            "missing_evidence": missing,
            "primary_suggestion": primary,
            "secondary_suggestions": list(classification.get("secondary_suggestions") or []),
            "fallback_only_if_not_resolved": fallback,
            "matched_anomalies": matched,
            "anomaly_context": classification.get("anomaly_context", {}),
            "component_groups": classification.get("component_groups", []),
            "candidate_component_groups": classification.get("candidate_component_groups", []),
            "task_profile": classification.get("task_profile", {}),
            "scope_rule": classification.get("scope_rule"),
            "filtered_anomalies": classification.get("filtered_anomalies", {}),
            "system_action_note": self._system_action_note(context),
            "failure_observation": self._build_failure_observation(context),
            "anomaly_evidence": self._build_anomaly_evidence(classification),
            "selected_fault_modules": self._select_fault_modules_fallback(context, classification),
        }
        result["advice_text"] = self._render_text(result)
        return result

    def _normalize_failure_observation(self, value: Any, context: Dict[str, Any]) -> Dict[str, Any]:
        fallback = self._build_failure_observation(context)
        if not isinstance(value, dict):
            return fallback
        normalized = dict(fallback)
        normalized.update({key: val for key, val in value.items() if val is not None})
        normalized["failed_criteria"] = self._normalize_text_list(normalized.get("failed_criteria"))
        return normalized

    def _normalize_anomaly_evidence(self, value: Any, classification: Dict[str, Any]) -> Dict[str, Any]:
        fallback = self._build_anomaly_evidence(classification)
        if not isinstance(value, dict):
            return fallback
        normalized = dict(fallback)
        normalized.update({key: val for key, val in value.items() if val is not None})
        if normalized.get("status") not in {"confirmed", "suspected", "none"}:
            normalized["status"] = fallback.get("status", "none")
        normalized["basis"] = self._normalize_text_list(normalized.get("basis"))
        normalized["matched_anomaly_types"] = self._normalize_text_list(normalized.get("matched_anomaly_types"))
        return normalized

    def _build_failure_observation(self, context: Dict[str, Any]) -> Dict[str, Any]:
        subtask = context.get("current_subtask") or {}
        failed = self._normalize_text_list(context.get("failed_criteria"))
        subtask_id = subtask.get("subtask_id")
        subtask_name = subtask.get("name")
        if failed:
            summary = f"子任务 {subtask_id}“{subtask_name}”未满足完成判据：{', '.join(failed)}。"
        else:
            summary = f"子任务 {subtask_id}“{subtask_name}”当前未达到完成条件。"
        return {
            "subtask_id": subtask_id,
            "subtask_name": subtask_name,
            "subtask_status": subtask.get("status"),
            "failed_criteria": failed,
            "summary": summary,
        }

    def _build_anomaly_evidence(self, classification: Dict[str, Any]) -> Dict[str, Any]:
        matched = [item for item in classification.get("matched_anomalies") or [] if isinstance(item, dict)]
        anomaly_context = classification.get("anomaly_context") if isinstance(classification.get("anomaly_context"), dict) else {}
        matched_types = self._normalize_text_list(anomaly_context.get("matched_anomaly_types"))
        if not matched_types:
            matched_types = [str(item.get("anomaly_type")) for item in matched if item.get("anomaly_type")]
        basis = self._normalize_text_list(classification.get("evidence_used"))
        if matched:
            status = "confirmed"
            note = "已有异常状态与当前子任务诊断范围形成对应关系。"
        elif classification.get("advice_generated"):
            status = "suspected"
            note = "当前诊断信息不足，建议作为排查方向参考。"
        else:
            status = "none"
            note = "当前只能确认任务完成条件未达成，尚无明确机器人或环境侧异常证据。"
        return {
            "status": status,
            "basis": basis,
            "matched_anomaly_types": matched_types,
            "note": note,
        }

    def _normalize_selected_fault_modules(
        self,
        selected: Any,
        context: Dict[str, Any],
        classification: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        """Keep LLM module choices inside the rule-provided candidate scope."""
        candidate_groups = self._candidate_group_map(classification)
        if not candidate_groups:
            return []
        if not isinstance(selected, list):
            return self._select_fault_modules_fallback(context, classification)

        normalized: list[Dict[str, Any]] = []
        for item in selected:
            if not isinstance(item, dict):
                continue
            key = item.get("module_key") or item.get("module_name") or item.get("name")
            if key not in candidate_groups:
                name = item.get("module_name")
                key = next(
                    (candidate_key for candidate_key, group in candidate_groups.items()
                     if group.get("name") == name),
                    None,
                )
            if key not in candidate_groups:
                continue

            group = candidate_groups[key]
            normalized.append({
                "module_key": key,
                "module_name": group.get("name", key),
                "related_anomaly_type": item.get("related_anomaly_type") or classification.get("anomaly_type"),
                "confidence": item.get("confidence") or classification.get("confidence", "medium"),
                "reason": item.get("reason") or self._module_reason(context, classification, group),
                "evidence": self._normalize_text_list(item.get("evidence")) or self._module_evidence(context, classification),
                "suggested_checks": self._normalize_text_list(item.get("suggested_checks")) or self._module_checks(group, classification),
            })

        return normalized or self._select_fault_modules_fallback(context, classification)

    def _select_fault_modules_fallback(
        self,
        context: Dict[str, Any],
        classification: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        if not classification.get("advice_generated"):
            return []

        candidate_groups = self._candidate_group_map(classification)
        if not candidate_groups:
            return []

        preferred_key = self._preferred_group_key(context, classification, candidate_groups)
        group = candidate_groups.get(preferred_key) or next(iter(candidate_groups.values()))
        module_key = group.get("key") or preferred_key or group.get("name")
        return [{
            "module_key": module_key,
            "module_name": group.get("name", module_key),
            "related_anomaly_type": classification.get("anomaly_type"),
            "confidence": classification.get("confidence", "medium"),
            "reason": self._module_reason(context, classification, group),
            "evidence": self._module_evidence(context, classification),
            "suggested_checks": self._module_checks(group, classification),
        }]

    def _candidate_group_map(self, classification: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for group in classification.get("candidate_component_groups") or []:
            if not isinstance(group, dict):
                continue
            key = group.get("key") or group.get("name")
            if key:
                groups[str(key)] = group
        for anomaly in classification.get("matched_anomalies") or []:
            if not isinstance(anomaly, dict):
                continue
            for group in anomaly.get("candidate_component_group_details") or []:
                if not isinstance(group, dict):
                    continue
                key = group.get("key") or group.get("name")
                if key and str(key) not in groups:
                    groups[str(key)] = group
        return groups

    def _preferred_group_key(
        self,
        context: Dict[str, Any],
        classification: Dict[str, Any],
        candidate_groups: Dict[str, Dict[str, Any]],
    ) -> Optional[str]:
        anomaly_type = classification.get("anomaly_type")
        subtask = context.get("current_subtask") or {}
        subtask_id = str(subtask.get("subtask_id") or "").upper()

        if anomaly_type == "perception":
            preferences = ["感知系统", "导航定位系统"]
        elif anomaly_type == "planning":
            preferences = ["规划决策系统", "机械臂与作业系统", "导航定位系统"]
        elif anomaly_type == "execution":
            if subtask_id in {"S4", "S6"}:
                preferences = ["机械臂与作业系统", "通信与接口系统"]
            elif subtask_id in {"S1", "S8"}:
                preferences = ["推进与运动控制系统", "机械臂与作业系统"]
            else:
                preferences = ["机械臂与作业系统", "推进与运动控制系统"]
        elif anomaly_type == "plant":
            preferences = ["机械臂与作业系统", "感知系统"]
        elif anomaly_type == "verification":
            preferences = ["感知系统", "机械臂与作业系统"]
        elif anomaly_type == "data_commun":
            preferences = ["通信与接口系统"]
        else:
            preferences = []

        for preferred in preferences:
            if preferred in candidate_groups:
                return preferred
            for key, group in candidate_groups.items():
                if group.get("name") == preferred:
                    return key
        return next(iter(candidate_groups.keys()), None)

    def _module_reason(
        self,
        context: Dict[str, Any],
        classification: Dict[str, Any],
        group: Dict[str, Any],
    ) -> str:
        subtask = context.get("current_subtask") or {}
        failed = context.get("failed_criteria") or []
        category = classification.get("exception_category") or classification.get("anomaly_type") or "异常"
        group_name = group.get("name") or group.get("key") or "候选系统"
        reason_parts = [
            f"当前子任务 {subtask.get('subtask_id')}“{subtask.get('name')}”与{group_name}存在直接职责关联",
            f"当前排查方向为{category}",
        ]
        if failed:
            reason_parts.append(f"未满足判据包括 {', '.join(str(item) for item in failed)}")
        return "；".join(reason_parts) + "。"

    def _module_evidence(self, context: Dict[str, Any], classification: Dict[str, Any]) -> list[str]:
        subtask = context.get("current_subtask") or {}
        evidence = [
            f"当前子任务 {subtask.get('subtask_id')}：{subtask.get('name')}",
        ]
        failed = context.get("failed_criteria") or []
        if failed:
            evidence.append("未满足判据：" + ", ".join(str(item) for item in failed))
        anomaly_type = classification.get("anomaly_type")
        if anomaly_type:
            evidence.append(f"匹配异常类型：{anomaly_type}")
        evidence.extend(str(item) for item in classification.get("evidence_used") or [])
        return evidence

    def _module_checks(self, group: Dict[str, Any], classification: Dict[str, Any]) -> list[str]:
        group_name = group.get("name") or group.get("key") or "相关系统"
        if "感知" in group_name:
            return [f"优先检查{group_name}的图像/声呐输入、目标识别结果和位姿估计稳定性。"]
        if "导航" in group_name or "定位" in group_name:
            return [f"优先检查{group_name}的位置、姿态和状态估计稳定性。"]
        if "规划" in group_name:
            return [f"优先检查{group_name}的目标位姿、约束条件和路径可达性。"]
        if "机械臂" in group_name or "作业" in group_name or "执行" in group_name:
            return [f"优先检查{group_name}的动作反馈、末端执行器状态和执行稳定性。"]
        if "通信" in group_name or "接口" in group_name:
            return [f"优先检查{group_name}的状态上报完整性、延迟和同步情况。"]
        return [f"优先检查{group_name}的状态反馈和现场执行条件。"]

    def _normalize_text_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if item is not None]
        if value is None:
            return []
        return [str(value)]

    def _task_status_summary(self, subtask: Dict[str, Any]) -> str:
        return (
            f"当前子任务 {subtask.get('subtask_id')}（{subtask.get('name')}）"
            f"状态为 {subtask.get('status')}。"
        )

    def _summary_text(self, subtask: Dict[str, Any], classification: Dict[str, Any]) -> str:
        if not classification.get("advice_generated"):
            return classification.get("summary") or "当前没有匹配到可输出的异常建议。"
        category = classification.get("exception_category", "异常")
        stage_meaning = classification.get("stage_meaning")
        if stage_meaning:
            return f"当前子任务 {subtask.get('subtask_id')}“{subtask.get('name')}”可能存在{category}相关风险。{stage_meaning}"
        return f"当前子任务 {subtask.get('subtask_id')}“{subtask.get('name')}”可能存在{category}相关风险。"

    def _cause_analysis(self, classification: Dict[str, Any]) -> list[str]:
        if not classification.get("advice_generated"):
            return []
        stage_meaning = classification.get("stage_meaning")
        if stage_meaning:
            return [stage_meaning]
        return []

    def _system_action_note(self, context: Dict[str, Any]) -> str:
        action = context.get("system_action")
        if not action:
            return "以上建议只用于现场排查，不表示系统已经执行重试、回退或终止任务。"
        return f"系统原有异常处理结果为 {action}；以下建议只用于现场排查，不改变该结果。"

    def _render_text(self, result: Dict[str, Any]) -> str:
        lines = []
        failure = result.get("failure_observation") if isinstance(result.get("failure_observation"), dict) else {}
        evidence = result.get("anomaly_evidence") if isinstance(result.get("anomaly_evidence"), dict) else {}

        failure_summary = failure.get("summary") or result.get("task_status_summary")
        if failure_summary:
            lines.append(f"当前失败点：{failure_summary}")

        selected_modules = [item for item in (result.get("selected_fault_modules") or []) if isinstance(item, dict)]
        evidence_status = evidence.get("status")
        if evidence_status == "none" or not result.get("advice_generated"):
            lines.append("异常关注点：目前还不能确认存在机器人内部或环境侧异常，建议先按失败判据复核现场状态和上报数据。")
        elif selected_modules:
            module_lines = []
            for item in selected_modules:
                module_name = item.get("module_name") or item.get("module_key")
                reason = item.get("reason")
                if module_name:
                    module_lines.append(f"{module_name}（{reason or '与当前失败现象直接相关'}）")
            if module_lines:
                lines.append("异常关注点：当前建议优先关注" + "；".join(module_lines))
        else:
            note = evidence.get("note") or "当前诊断信息不足，建议作为排查方向参考。"
            lines.append(f"异常关注点：{note}")

        checks = []
        for item in selected_modules:
            checks.extend(str(check) for check in item.get("suggested_checks") or [])
        primary_text = (result.get("primary_suggestion") or {}).get("text")
        if primary_text:
            checks.append(str(primary_text))
        checks.extend(str(item) for item in result.get("secondary_suggestions") or [])
        if checks:
            lines.append("处理建议：" + "；".join(dict.fromkeys(checks)))

        missing = result.get("missing_evidence") or []
        if missing:
            lines.append("仍需补充的信息：" + "、".join(str(item) for item in missing))

        fallback = result.get("fallback_only_if_not_resolved") or []
        if fallback:
            parts = [
                f"{item.get('target_subtask')}（{item.get('condition')}，{item.get('impact')}）"
                for item in fallback
            ]
            lines.append("如果仍无法解决，再考虑：" + "；".join(parts))

        lines.append(result.get("system_action_note", ""))
        return "\n".join(line for line in lines if line)
