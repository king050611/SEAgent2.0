"""
task_manager.py – Task state machine, coordinating subtask progression, anomaly handling, and manual approval.
Uses atomic updates to ensure concurrency safety.
"""

import time
import logging
from typing import Dict, Any, List, Optional
from .state_store import StateStore
from .task_decomposer import TaskDecomposer
from .criteria_evaluator import CriteriaEvaluator
from .anomaly_handler import AnomalyHandler
from .intervention_handler import InterventionHandler
from .state_monitor import StateMonitor
from .query_responder import QueryResponder

logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, state_store: StateStore, task_decomposer: TaskDecomposer,
                 criteria_evaluator: CriteriaEvaluator, anomaly_handler: AnomalyHandler,
                 intervention_handler: InterventionHandler, state_monitor: StateMonitor,
                 # ############# anomaly advisor 接入开始 #############
                 anomaly_advisor: Optional[Any] = None):
                 # ############# anomaly advisor 接入结束 #############
        self.state_store = state_store
        self.task_decomposer = task_decomposer
        self.criteria_evaluator = criteria_evaluator
        self.anomaly_handler = anomaly_handler
        self.intervention_handler = intervention_handler
        self.state_monitor = state_monitor
        self.query_responder = QueryResponder(intervention_handler.llm)
        # ############# anomaly advisor 接入开始 #############
        self.anomaly_advisor = anomaly_advisor
        # ############# anomaly advisor 接入结束 #############

    # ---------- Task Creation ----------
    def create_new_task(self, task_id: str, description: str, initial_data: Dict) -> Dict[str, Any]:
        """Create a new task, decompose into subtasks, initialize state, and save intent data."""
        if self.state_store.get_task(task_id):
            return {"ok": False, "error": "task already exists", "task_id": task_id}

        subtasks = self.task_decomposer.decompose(description, initial_data)
        if not subtasks:
            return {"ok": False, "error": "no subtasks configured"}

        subtasks = self._normalize_subtask_order(subtasks)
        task_state = {
            "task_id": task_id,
            "description": description,
            "created_at": time.time(),
            "overall_status": "in_progress",
            "current_subtask": subtasks[0]["subtask_id"] if subtasks else None,
            "subtasks": subtasks,
            "metadata": initial_data,
            "global_parameters": initial_data or {},
            # ############# anomaly_state 接入开始 #############
            "anomaly_state": (initial_data or {}).get("anomaly_state", {}),
            "latest_anomaly_context": None,
            "latest_anomaly_advice": None,
            # ############# anomaly_state 接入结束 #############
        }
        self.state_store.save_task(task_id, task_state)
        if subtasks:
            self._start_subtask(task_id, subtasks[0]["subtask_id"])
        logger.info(f"New task created: {task_id} - {description}")
        return {"ok": True, "task_id": task_id}

    def _start_subtask(self, task_id: str, subtask_id: str):
        """Atomically change a pending subtask status to in_progress."""
        def _start_update(state):
            for st in state["subtasks"]:
                if st["subtask_id"] == subtask_id:
                    if st["status"] == "pending":
                        # ############# anomaly advice 生命周期清理开始 #############
                        self._clear_anomaly_advice_fields(state)
                        # ############# anomaly advice 生命周期清理结束 #############
                        st["status"] = "in_progress"
                        st["retry_count"] = 0
                        st["completion_criteria"] = {}
                        st["evidence_summary"] = ""
                        st["latest_state"] = {}
                        logger.info(f"Task {task_id} started subtask {subtask_id}")
                    else:
                        logger.debug(f"Task {task_id} subtask {subtask_id} status is {st['status']}, not starting")
                    break
        updated = self.state_store.update_task_atomic(task_id, _start_update)
        if updated is None:
            logger.error(f"Task {task_id} not found when trying to start {subtask_id}")

    # ---------- Status Update & Progression ----------
    def update_subtask_status(self, task_id: str, subtask_id: str, status: str,
                              criteria_met: List[str], criteria_details: Dict, evidence_summary: str,
                              anomaly_key: Optional[str] = None,
                              # ############# anomaly_state 接入开始 #############
                              anomaly_state: Optional[Dict[str, Any]] = None) -> Dict:
                              # ############# anomaly_state 接入结束 #############
        """
        Entry point for status reports. The actual evaluation is performed by StateMonitor.
        当上游能明确提供 anomaly_key 时，优先使用，减少异常路径歧义。
        """
        if not subtask_id:
            return {"error": "subtask_id required"}
        if status and status not in ("in_progress", "running", "reported", "completed", "failed"):
            return {"error": "invalid status", "allowed": ["in_progress", "running", "reported", "completed", "failed"]}
        if criteria_details is None or not isinstance(criteria_details, dict):
            return {"error": "criteria_details must be an object"}

        # ############# anomaly_state 接入开始 #############
        if anomaly_state is not None:
            if not isinstance(anomaly_state, dict):
                return {"error": "anomaly_state must be an object"}
            self._update_anomaly_state(task_id, anomaly_state)
        # ############# anomaly_state 接入结束 #############

        monitor_result = self.state_monitor.update_subtask_state(task_id, subtask_id, criteria_details)
        if monitor_result.get("error"):
            return monitor_result

        task_state = self.state_store.get_task(task_id)
        if not task_state:
            return {"error": "task not found"}

        target = next((s for s in task_state["subtasks"] if s["subtask_id"] == subtask_id), None)
        if not target:
            return {"error": "subtask not found"}

        if evidence_summary:
            self._append_evidence_summary(task_id, subtask_id, evidence_summary)

        if target["status"] == "completed":
            # ############# anomaly advice 生命周期清理开始 #############
            self._clear_anomaly_advice(task_id)
            # ############# anomaly advice 生命周期清理结束 #############
            return self._advance_to_next(task_id, subtask_id)
        elif target["status"] == "failed":
            criteria = target.get("completion_criteria", {}) or {}
            unmet = criteria.get("hard_unmet_details", [])
            resolved_anomaly_key = anomaly_key or self._infer_anomaly_key(subtask_id, unmet)
            anomaly_result = self.anomaly_handler.handle_anomaly(task_id, subtask_id, resolved_anomaly_key, unmet)
            # ############# anomaly advisor 接入开始 #############
            self._capture_anomaly_advice_for_unmet_criteria(
                task_id=task_id,
                task_state=task_state,
                subtask=target,
                criteria=criteria,
                system_action=anomaly_result,
            )
            # ############# anomaly advisor 接入结束 #############
            if anomaly_result.get("action") == "rollback":
                to_sub = anomaly_result.get("to_subtask")
                if to_sub:
                    self._start_subtask(task_id, to_sub)
            elif anomaly_result.get("action") == "retry":
                pass  # status already set to in_progress by anomaly_handler
            return anomaly_result
        elif target["status"] == "waiting_approval":
            # ############# anomaly advice 生命周期清理开始 #############
            criteria = target.get("completion_criteria") or {}
            if criteria.get("all_met") is True:
                self._clear_anomaly_advice(task_id)
            # ############# anomaly advice 生命周期清理结束 #############
            return {"action": "waiting_approval", "subtask_id": subtask_id}
        else:
            # ############# anomaly advisor 接入开始 #############
            criteria = target.get("completion_criteria", {}) or {}
            if criteria.get("all_met") is False:
                self._capture_anomaly_advice_for_unmet_criteria(
                    task_id=task_id,
                    task_state=task_state,
                    subtask=target,
                    criteria=criteria,
                    system_action={
                        "action": "diagnose_only",
                        "reason": "criteria_unmet_but_not_failed",
                        "message": "判据尚未全部满足，任务保持进行中，仅生成诊断建议。",
                    },
                )
            # ############# anomaly advisor 接入结束 #############
            return {"ok": True, "message": "subtask still in progress"}

    def _advance_to_next(self, task_id: str, current_subtask_id: str) -> Dict:
        """Atomically advance to the next subtask and start it."""
        def _advance_update(state):
            subtasks = state["subtasks"]
            idx = None
            for i, st in enumerate(subtasks):
                if st["subtask_id"] == current_subtask_id:
                    idx = i
                    break
            if idx is None:
                return
            if idx + 1 < len(subtasks):
                next_sub = subtasks[idx + 1]["subtask_id"]
                if subtasks[idx + 1]["status"] in ("completed", "failed", "waiting_approval"):
                    logger.warning(f"Task {task_id} next subtask {next_sub} already in terminal state {subtasks[idx+1]['status']}, cannot auto-advance")
                    return
                if subtasks[idx + 1]["status"] != "pending":
                    subtasks[idx + 1]["status"] = "pending"
                    subtasks[idx + 1]["retry_count"] = 0
                    subtasks[idx + 1]["completion_criteria"] = {}
                    subtasks[idx + 1]["evidence_summary"] = ""
                    subtasks[idx + 1]["latest_state"] = {}
                state["current_subtask"] = next_sub
                logger.info(f"Task {task_id} advanced from {current_subtask_id} to {next_sub}")
            else:
                state["overall_status"] = "completed"
                # ############# anomaly advice 生命周期清理开始 #############
                self._clear_anomaly_advice_fields(state)
                # ############# anomaly advice 生命周期清理结束 #############
                logger.info(f"Task {task_id} completed")

        updated_state = self.state_store.update_task_atomic(task_id, _advance_update)
        if updated_state is None:
            return {"error": "task not found"}

        subtasks = updated_state["subtasks"]
        idx = None
        for i, st in enumerate(subtasks):
            if st["subtask_id"] == current_subtask_id:
                idx = i
                break
        if idx is not None and idx + 1 < len(subtasks):
            next_sub = subtasks[idx + 1]["subtask_id"]
            self._start_subtask(task_id, next_sub)
            return {"action": "advance", "next_subtask": next_sub}
        else:
            return {"action": "task_completed"}

    def _infer_anomaly_key(self, subtask_id: str, unmet_criteria: List[str]) -> str:
        """
        Map unmet criteria to an anomaly key based on task template.
        优先使用模板中显式配置的异常；若仅凭判据无法可靠判断，则退回 manual_intervention。
        """
        subtask_tpl = None
        for st in self.task_decomposer.subtask_templates:
            if st["id"] == subtask_id:
                subtask_tpl = st
                break
        if not subtask_tpl:
            return "manual_intervention"

        anomalies_map = subtask_tpl.get("anomalies", {})
        if not anomalies_map:
            return "manual_intervention"

        # 先尝试原有的字符串包含匹配
        for anomaly_key in anomalies_map.keys():
            for unmet in unmet_criteria:
                if unmet in anomaly_key or anomaly_key in unmet:
                    return anomaly_key

        # 对当前模板做最小启发式补全，避免关键失败路径全部退化为人工介入
        heuristic_mapping = {
            "distance_error_max": "distance_error_timeout",
            "angle_error_max": "angle_error_timeout",
            "speed_stable_frames": "speed_not_stable",
            "slot_pose_delta_max": "estimation_unstable",
            "plug_pose_delta_max": "estimation_unstable",
            "ik_valid_flag": "ik_no_solution",
            "grasp_done_flag": "grasp_failed",
            "insert_done_flag": "insert_failed",
            "visual_check_flag": "visual_check_failed",
            "arm_reset_flag": "arm_reset_failed",
            "return_position_error_max": "return_position_error",
        }
        for unmet in unmet_criteria:
            candidate = heuristic_mapping.get(unmet)
            if candidate and candidate in anomalies_map:
                return candidate

        # 某些步骤只有单一路径异常，可保守地直接选用
        if len(anomalies_map) == 1:
            return next(iter(anomalies_map.keys()))

        return "manual_intervention"


    # ############# anomaly advisor 接入开始 #############
    FAILURE_ADVICE_TRIGGER_FIELDS = {
        "distance_error_max",
        "angle_error_max",
        "speed_stable_frames",
        "min_grid_count",
        "panel_visible_flag",
        "slot_pose_delta_max",
        "plug_pose_delta_max",
        "slot_stable_flag",
        "plug_stable_flag",
        "ik_valid_flag",
        "grasp_done_flag",
        "insert_done_flag",
        "visual_check_flag",
        "arm_reset_flag",
        "return_position_error_max",
    }

    def _update_anomaly_state(self, task_id: str, anomaly_state: Dict[str, Any]):
        """Persist task-level anomaly_state as the current backend anomaly snapshot."""
        def _update(state: Dict[str, Any]):
            state["anomaly_state"] = anomaly_state

        updated = self.state_store.update_task_atomic(task_id, _update)
        if updated is None:
            logger.warning("Task %s not found when updating anomaly_state", task_id)

    def _clear_anomaly_advice_fields(self, state: Dict[str, Any]):
        """Clear task-level anomaly advice snapshot after a subtask no longer fails."""
        state["anomaly_state"] = {}
        state["latest_anomaly_context"] = None
        state["latest_anomaly_advice"] = None

    def _clear_anomaly_advice(self, task_id: str):
        """Clear persisted anomaly advice for successful completion/progression."""
        def _clear(state: Dict[str, Any]):
            self._clear_anomaly_advice_fields(state)

        updated = self.state_store.update_task_atomic(task_id, _clear)
        if updated is None:
            logger.warning("Task %s not found when clearing anomaly advice", task_id)

    def _should_capture_anomaly_advice(self, criteria: Dict[str, Any]) -> bool:
        hard_unmet = criteria.get("hard_unmet_details", []) or []
        soft_unmet = criteria.get("soft_unmet_details", []) or []
        unmet_fields = set(hard_unmet) | set(soft_unmet)
        return bool(unmet_fields.intersection(self.FAILURE_ADVICE_TRIGGER_FIELDS))

    def _capture_anomaly_advice_for_unmet_criteria(
        self,
        task_id: str,
        task_state: Dict[str, Any],
        subtask: Dict[str, Any],
        criteria: Dict[str, Any],
        system_action: Dict[str, Any],
    ):
        if not self._should_capture_anomaly_advice(criteria):
            return
        failed_criteria = (
            list(criteria.get("hard_unmet_details", []) or [])
            + list(criteria.get("soft_unmet_details", []) or [])
        )
        self._capture_anomaly_advice(
            task_id=task_id,
            task_state=task_state,
            subtask=subtask,
            failed_criteria=failed_criteria,
            system_action=system_action,
        )

    def _capture_anomaly_advice(
        self,
        task_id: str,
        task_state: Dict[str, Any],
        subtask: Dict[str, Any],
        failed_criteria: List[str],
        system_action: Dict[str, Any],
    ):
        """Generate and persist anomaly advice when criteria failure is detected."""
        if not self.anomaly_advisor:
            return
        try:
            context = self.anomaly_advisor.record_anomaly_context(
                task_state=task_state,
                subtask=subtask,
                failed_criteria=failed_criteria,
                anomaly_key=None,
                system_action=system_action,
            )
            advice = self.anomaly_advisor.generate_advice(context)
        except Exception as exc:
            logger.exception("Failed to generate anomaly advice for task %s subtask %s: %s", task_id, subtask.get("subtask_id"), exc)
            return

        def _store(state: Dict[str, Any]):
            state["latest_anomaly_context"] = context
            state["latest_anomaly_advice"] = advice

        updated = self.state_store.update_task_atomic(task_id, _store)
        if updated is None:
            logger.warning("Task %s not found when storing anomaly advice", task_id)
    # ############# anomaly advisor 接入结束 #############

    # ---------- Manual Approval ----------
    def approve_subtask(self, task_id: str, subtask_id: str) -> Dict:
        """Approve a subtask that is waiting for manual approval, then advance."""
        result = {}

        def _approve_update(state):
            nonlocal result
            if subtask_id != state.get("current_subtask"):
                result = {
                    "error": "subtask_out_of_order",
                    "message": f"当前应审核 {state.get('current_subtask')}，不能审核 {subtask_id}。",
                }
                return
            target = next((s for s in state["subtasks"] if s["subtask_id"] == subtask_id), None)
            if not target:
                result = {"error": "subtask not found"}
                return
            if target["status"] != "waiting_approval":
                result = {"error": "subtask_not_waiting_approval", "status": target.get("status")}
                return
            # ############# approval 状态语义区分开始 #############
            criteria = target.get("completion_criteria") or {}
            if criteria.get("all_met") is not True:
                result = {
                    "error": "approval_not_allowed_for_unmet_criteria",
                    "message": "当前子任务处于异常人工介入状态，判据尚未满足；请先重试或回退后重新上报数据，或使用人工完成/强制完成流程。",
                    "status": target.get("status"),
                    "hard_met": criteria.get("hard_met"),
                    "soft_met": criteria.get("soft_met"),
                    "hard_unmet_details": criteria.get("hard_unmet_details", []),
                }
                return
            # ############# approval 状态语义区分结束 #############
            target["status"] = "completed"
            result = {"ok": True}
            logger.info(f"Task {task_id} subtask {subtask_id} approved manually")

        updated = self.state_store.update_task_atomic(task_id, _approve_update)
        if updated is None:
            return {"error": "task not found"}
        if result.get("error"):
            return result
        return self._advance_to_next(task_id, subtask_id)

    # ---------- Pending Intervention Confirmation ----------
    def set_pending_intervention(self, task_id: str, action: Dict[str, Any], user_message: str, raw_intent: Dict[str, Any] = None) -> Dict[str, Any]:
        """Persist an intervention action that must be confirmed by the user before execution."""
        result: Dict[str, Any] = {}

        def _set_update(state: Dict[str, Any]):
            nonlocal result
            state["pending_intervention"] = {
                "action": action,
                "user_message": user_message,
                "raw_intent": raw_intent or {},
                "created_at": time.time(),
                "status": "awaiting_confirmation",
            }
            result = {"ok": True, "pending_intervention": state["pending_intervention"]}

        updated = self.state_store.update_task_atomic(task_id, _set_update)
        if updated is None:
            return {"error": "task not found"}
        return result

    def clear_pending_intervention(self, task_id: str) -> Dict[str, Any]:
        """Remove a pending intervention after it is confirmed, cancelled, or superseded."""
        result: Dict[str, Any] = {}

        def _clear_update(state: Dict[str, Any]):
            nonlocal result
            pending = state.pop("pending_intervention", None)
            result = {"ok": True, "cleared": bool(pending), "pending_intervention": pending}

        updated = self.state_store.update_task_atomic(task_id, _clear_update)
        if updated is None:
            return {"error": "task not found"}
        return result

    def get_pending_intervention(self, task_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the active pending intervention object from task state, if any."""
        pending = task_state.get("pending_intervention") if task_state else None
        if not isinstance(pending, dict):
            return None
        if pending.get("status") != "awaiting_confirmation":
            return None
        if not isinstance(pending.get("action"), dict):
            return None
        return pending

    # ---------- Intervention ----------
    def execute_intervention(self, task_id: str, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute an intervention action that was parsed by LLM.
        action format: {"action": "rollback", "to_subtask": "S2"} or {"action": "force_complete", "subtask_id": "S1"}
        """
        action_type = action.get("action")
        if action_type == "force_complete":
            subtask_id = action.get("subtask_id")
            if not subtask_id:
                return {"error": "missing subtask_id", "action": action_type}
            return self._force_complete_subtask(task_id, subtask_id)
        else:
            return self.intervention_handler.apply_intervention(task_id, action)

    def _force_complete_subtask(self, task_id: str, subtask_id: str) -> Dict[str, Any]:
        """
        Manually mark the current subtask as completed, then advance the serial flow.
        This is intentionally restricted to current_subtask to preserve strict ordering.
        Now supports recovery from overall 'failed' state.
        """
        result: Dict[str, Any] = {}

        def _complete_update(state: Dict[str, Any]):
            nonlocal result
            # 只禁止已完成的任务强制完成；失败状态允许恢复
            if state.get("overall_status") == "completed":
                result = {
                    "error": "task_not_active",
                    "message": "任务已完成，不能人工完成子任务；如需重做请先回退。",
                    "action": "force_complete",
                }
                return

            current = state.get("current_subtask")
            if subtask_id != current:
                result = {
                    "error": "force_complete_out_of_order",
                    "message": f"流程必须严格串行。当前子任务是 {current}，不能直接人工完成 {subtask_id}。",
                    "action": "force_complete",
                    "current_subtask": current,
                    "reported_subtask": subtask_id,
                }
                return

            target = next((st for st in state.get("subtasks", []) if st.get("subtask_id") == subtask_id), None)
            if not target:
                result = {"error": f"subtask {subtask_id} not found", "action": "force_complete"}
                return

            # 允许强制完成的状态：in_progress, waiting_approval, failed
            if target.get("status") not in ("in_progress", "waiting_approval", "failed"):
                result = {
                    "error": "subtask_not_force_completable",
                    "message": f"子任务 {subtask_id} 当前状态为 {target.get('status')}，不适合人工完成。",
                    "action": "force_complete",
                }
                return

            # 如果任务整体是 failed，重置为 in_progress
            if state.get("overall_status") == "failed":
                state["overall_status"] = "in_progress"

            target["status"] = "completed"
            target.setdefault("completion_criteria", {})["manual_forced"] = True
            target["evidence_summary"] = (target.get("evidence_summary") or "") + "\n[人工完成] 用户确认该子任务完成。"
            result = {"action": "force_complete", "subtask_id": subtask_id, "success": True}

        updated = self.state_store.update_task_atomic(task_id, _complete_update)
        if updated is None:
            return {"error": "task not found", "action": "force_complete"}
        if result.get("error"):
            return result

        advance_result = self._advance_to_next(task_id, subtask_id)
        result["advance"] = advance_result
        return result

    def _generate_mock_criteria_details(self, criteria_config: Dict[str, Any]) -> Dict[str, Any]:
        """Generate mock state data that satisfies all hard and soft criteria."""
        mock = {}
        for cat in ("hard", "soft"):
            for key, threshold in criteria_config.get(cat, {}).items():
                if isinstance(threshold, (int, float)):
                    if "max" in key or "delta" in key:
                        mock[key] = threshold / 2.0 if threshold > 0 else 0
                    elif "min" in key:
                        mock[key] = threshold
                    else:
                        mock[key] = threshold
                elif isinstance(threshold, bool):
                    mock[key] = threshold
                else:
                    mock[key] = threshold
        # Ensure integer fields are actually integers
        for key in list(mock.keys()):
            if "frames" in key:
                mock[key] = int(mock[key])
        return mock

    # ---------- Query & Reset ----------
    def get_task_status(self, task_id: str) -> Optional[Dict]:
        """Return the full task state."""
        return self.state_store.get_task(task_id)

    def query_progress(self, task_id: str, user_question: str) -> str:
        """Legacy method kept for compatibility; uses QueryResponder directly."""
        task_status = self.state_store.get_task(task_id)
        if not task_status:
            return "任务不存在"
        # This method is not used in the new flow; kept for external calls.
        return self.query_responder.generate_reply(
            reply_intent="回答用户关于当前任务进度、状态、判据或下一步的问题",
            user_message=user_question,
            task_state=task_status,
        )

    def reset_task(self, task_id: str):
        """Delete the task state."""
        self.state_store.delete_task(task_id)
        logger.info(f"Task {task_id} reset (deleted)")

    # ---------- Helpers ----------
    def _normalize_subtask_order(self, subtasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort subtasks by numeric S order."""
        def order_key(st: Dict[str, Any]):
            sid = st.get("subtask_id", "")
            if isinstance(sid, str) and sid.upper().startswith("S") and sid[1:].isdigit():
                return int(sid[1:])
            return 10_000
        return sorted(subtasks, key=order_key)

    def _append_evidence_summary(self, task_id: str, subtask_id: str, extra_summary: str):
        """在系统总结基础上附加上游证据摘要，保留最小侵入的外部信息。"""
        def _update(state: Dict[str, Any]):
            for st in state.get("subtasks", []):
                if st.get("subtask_id") == subtask_id:
                    base = st.get("evidence_summary") or ""
                    if extra_summary not in base:
                        st["evidence_summary"] = (base + ("\n" if base else "") + extra_summary).strip()
                    break

        updated = self.state_store.update_task_atomic(task_id, _update)
        if updated is None:
            logger.warning("Task %s not found when appending evidence summary to %s", task_id, subtask_id)