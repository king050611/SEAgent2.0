"""
intervention_handler.py – 干预指令执行器（支持回退、参数修改、状态覆盖）
增加从 failed 状态恢复的能力。
"""

from typing import Dict, Any, Optional
from .state_store import StateStore
from .task_decomposer import TaskDecomposer
from .criteria_evaluator import CriteriaEvaluator
import logging
import copy

logger = logging.getLogger(__name__)


class InterventionHandler:
    def __init__(self, llm, task_decomposer: TaskDecomposer, state_store: StateStore,
                 criteria_evaluator: CriteriaEvaluator):
        self.llm = llm
        self.task_decomposer = task_decomposer
        self.state_store = state_store
        self.criteria_evaluator = criteria_evaluator

    def apply_intervention(self, task_id: str, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply an intervention action. The action dict must have an "action" field.
        Supported actions: rollback, change_parameter, retry, override_field, force_complete.
        """
        result = {}
        action_type = action.get("action")
        print("当前干预动作为：", action_type)
        print("干预目标子任务为：", action.get("to_subtask"))
        def _apply_update(state: Dict[str, Any]):
            nonlocal result
            overall = state.get("overall_status")
            current_sub = state.get("current_subtask")
            print("当前子任务为：", current_sub)
            # 允许从 failed 状态恢复的干预动作：rollback, override_field, force_complete
            # 对于 override_field 和 force_complete，必须操作当前子任务，否则拒绝
            # if overall in ("completed", "failed"):
            #     if action_type not in ("rollback",):
            #         result = {"error": "task_not_active", "message": "任务已结束，只允许通过回退、覆盖字段或强制完成重新打开流程。"}
            #         return
            #     if action_type in ("override_field", "force_complete"):
            #         target_sub = action.get("subtask_id") or current_sub
            #         if target_sub != current_sub:
            #             result = {
            #                 "error": "invalid_subtask",
            #                 "message": f"当前失败子任务是 {current_sub}，不能直接干预 {target_sub}；请先回退到该子任务。"
            #             }
            #             return
            if overall == "completed":
                if action_type not in ("rollback",):
                    result = {
                        "error": "task_not_active",
                        "message": "任务已完成，只允许通过回退重新打开流程。"
                    }
                    return

            if overall == "failed":
                if action_type not in ("rollback", "override_field", "force_complete", "retry"):
                    result = {
                        "error": "task_not_active",
                        "message": "任务已结束，只允许通过回退、重试当前失败子任务、覆盖字段或强制完成重新打开流程。"
                    }
                    return

                if action_type in ("override_field", "force_complete", "retry"):
                    target_sub = action.get("subtask_id") or current_sub
                    if target_sub != current_sub:
                        result = {
                            "error": "invalid_subtask",
                            "message": f"当前失败子任务是 {current_sub}，不能直接干预 {target_sub}；请先回退到该子任务。"
                        }
                        return

            if action_type == "rollback":
                target_sub = action.get("to_subtask")
                if not self._subtask_exists(state, target_sub):
                    result = {"error": "invalid_subtask", "message": f"回退目标 {target_sub} 不存在。"}
                    return
                self._reset_from_subtask(state, target_sub)
                self._activate_subtask(state, target_sub, "人工确认回退后重新执行")
                state["current_subtask"] = target_sub
                state["overall_status"] = "in_progress"
                result = {"action": "rollback", "to_subtask": target_sub, "ok": True}

            elif action_type == "change_parameter":
                param = action.get("parameter")
                value = action.get("value")
                target_sub = action.get("subtask_id") or state.get("current_subtask")
                if not param or value is None or not self._subtask_exists(state, target_sub):
                    result = {"error": "invalid_parameter", "message": "参数名或值缺失，或子任务不存在"}
                    return

                state.setdefault("global_parameters", {})[param] = value
                for st in state.get("subtasks", []):
                    if st.get("subtask_id") == target_sub:
                        st.setdefault("parameters", {})[param] = value
                        break

                self._reset_from_subtask(state, target_sub)
                self._activate_subtask(state, target_sub, f"人工确认修改参数 {param} 后重新执行")
                state["current_subtask"] = target_sub
                state["overall_status"] = "in_progress"
                result = {"action": "change_parameter", "subtask_id": target_sub, "parameter": param, "value": value, "ok": True}

            elif action_type == "retry":
                subtask_id = action.get("subtask_id") or state.get("current_subtask")
                if not self._subtask_exists(state, subtask_id):
                    result = {"error": "invalid_subtask", "message": f"重试目标 {subtask_id} 不存在。"}
                    return
                current = state.get("current_subtask")
                if subtask_id != current:
                    result = {"error": "retry_out_of_order", "message": f"当前子任务是 {current}，不能直接重试 {subtask_id}；如需重做历史步骤请先回退。"}
                    return
                for st in state.get("subtasks", []):
                    if st.get("subtask_id") == subtask_id:
                        # ############# approval retry/rollback 清理开始 #############
                        self._clear_reapproval_runtime_state(state, subtask_id)
                        # ############# approval retry/rollback 清理结束 #############
                        st["status"] = "in_progress"
                        st["retry_count"] = 0
                        st["completion_criteria"] = {}
                        st["evidence_summary"] = "人工确认重试，当前步骤重新进入执行中"
                        st["latest_state"] = {}
                        st.pop("user_overrides", None)
                        break
                state["current_subtask"] = subtask_id
                state["overall_status"] = "in_progress"
                result = {"action": "retry", "subtask_id": subtask_id, "ok": True}

            elif action_type == "override_field":
                subtask_id = action.get("subtask_id") or state.get("current_subtask")
                field = action.get("field")
                value = action.get("value")
                if not field or value is None or not self._subtask_exists(state, subtask_id):
                    result = {"error": "missing_field_or_value", "message": "缺少字段名或值，或子任务不存在。"}
                    return

                target_st = None
                for st in state.get("subtasks", []):
                    if st.get("subtask_id") == subtask_id:
                        target_st = st
                        break
                if not target_st:
                    result = {"error": "subtask_not_found"}
                    return

                original_overall = state.get("overall_status")

                if "user_overrides" not in target_st:
                    target_st["user_overrides"] = {}
                target_st["user_overrides"][field] = value

                # 重新评估该子任务（合并 overrides 后）
                re_eval_result = self._reevaluate_subtask(state, subtask_id)
                result.update(re_eval_result)
                result["action"] = "override_field"
                result["field"] = field
                result["value"] = value

                new_sub_status = target_st.get("status")
                # 如果任务整体是 failed，且子任务不再是 failed，恢复整体状态
                if original_overall == "failed" and new_sub_status not in ("failed", None):
                    state["overall_status"] = "in_progress"
                    result["overall_status_restored"] = True
                    logger.info(f"Task {task_id} overall status restored from failed to in_progress after override field.")
                # 如果子任务已完成且当前子任务就是该任务，尝试推进
                if new_sub_status == "completed" and state.get("current_subtask") == subtask_id:
                    advance_res = self._advance_to_next(state, subtask_id)
                    result["advance"] = advance_res

            else:
                result = {"error": f"unknown action: {action_type}"}

        updated = self.state_store.update_task_atomic(task_id, _apply_update)
        if updated is None and "error" not in result:
            result = {"error": "task not found"}
        return result

    def _reevaluate_subtask(self, state: Dict[str, Any], subtask_id: str) -> Dict[str, Any]:
        """
        在原子更新内部重新评估子任务（已持有锁），根据判据更新状态。
        返回包含评估结果和可能的下一步动作。
        """
        subtasks = state.get("subtasks", [])
        target = next((st for st in subtasks if st["subtask_id"] == subtask_id), None)
        if not target:
            return {"error": "subtask not found"}

        criteria_ref = target.get("criteria_ref")
        if not criteria_ref:
            target["status"] = "completed"
            target["completion_criteria"] = {}
            target["evidence_summary"] = "未配置判据，强制完成"
            return {"ok": True, "action": "completed"}

        criteria_config = self.criteria_evaluator.get_criteria(criteria_ref)
        if not criteria_config:
            return {"error": "criteria_not_found", "message": f"缺少判据配置 {criteria_ref}"}

        latest = target.get("latest_state", {})
        overrides = target.get("user_overrides", {})
        merged_state = copy.deepcopy(latest)
        merged_state.update(overrides)

        evaluation = self.criteria_evaluator.evaluate(criteria_config, merged_state)
        target["completion_criteria"] = evaluation
        target["evidence_summary"] = self._build_evidence_summary(evaluation, merged_state)

        all_met = evaluation.get("all_met", False)
        require_approval = evaluation.get("require_approval", False)

        if all_met:
            if require_approval:
                target["status"] = "waiting_approval"
                return {"action": "waiting_approval", "message": "判据满足，等待人工审核"}
            else:
                target["status"] = "completed"
                if state.get("current_subtask") == subtask_id:
                    advance_result = self._advance_to_next(state, subtask_id)
                    return {"action": "advance", "advance_result": advance_result}
                return {"action": "completed"}
        else:
            if not evaluation.get("hard_met", False):
                target["status"] = "failed"
            else:
                target["status"] = "in_progress"
            return {"action": "still_incomplete", "hard_unmet": evaluation.get("hard_unmet_details", [])}

    def _advance_to_next(self, state: Dict[str, Any], current_subtask_id: str) -> Dict[str, Any]:
        """原地推进到下一个子任务，不写回存储（调用者已持有锁）。"""
        subtasks = state["subtasks"]
        idx = None
        for i, st in enumerate(subtasks):
            if st["subtask_id"] == current_subtask_id:
                idx = i
                break
        if idx is None or idx + 1 >= len(subtasks):
            state["overall_status"] = "completed"
            return {"action": "task_completed"}
        next_sub = subtasks[idx + 1]["subtask_id"]
        if subtasks[idx + 1]["status"] != "pending":
            subtasks[idx + 1]["status"] = "pending"
            subtasks[idx + 1]["retry_count"] = 0
            subtasks[idx + 1]["completion_criteria"] = {}
            subtasks[idx + 1]["evidence_summary"] = ""
            subtasks[idx + 1]["latest_state"] = {}
            subtasks[idx + 1].pop("user_overrides", None)
        state["current_subtask"] = next_sub
        self._activate_subtask(state, next_sub, "自动推进后激活")
        return {"next_subtask": next_sub}

    def _build_evidence_summary(self, criteria_result: Dict, merged_state: Dict) -> str:
        """生成证据摘要，标注使用了覆盖值。"""
        parts = []
        if criteria_result.get("all_met"):
            parts.append("所有判据满足（含用户覆盖值）")
        else:
            hard_unmet = criteria_result.get("hard_unmet_details", [])
            if hard_unmet:
                parts.append(f"硬判据不满足: {', '.join(hard_unmet)}")
            soft_unmet = criteria_result.get("soft_unmet_details", [])
            if soft_unmet:
                parts.append(f"软判据未满足: {', '.join(soft_unmet)}")
        if merged_state:
            parts.append("已记录状态字段: " + ", ".join(sorted(merged_state.keys())))
        if not parts:
            parts.append("状态数据已接收")
        return "; ".join(parts)

    def _subtask_exists(self, state: Dict[str, Any], subtask_id: Optional[str]) -> bool:
        return bool(subtask_id) and any(st.get("subtask_id") == subtask_id for st in state.get("subtasks", []))

    # ############# approval retry/rollback 清理开始 #############
    def _clear_reapproval_runtime_state(self, state: Dict[str, Any], subtask_id: str):
        """Invalidate stale approval/anomaly snapshots before retry or rollback reopens reporting."""
        state["anomaly_state"] = {}
        state["latest_anomaly_context"] = None
        state["latest_anomaly_advice"] = None
        notifications = state.get("notifications")
        if isinstance(notifications, list):
            state["notifications"] = [
                item for item in notifications
                if subtask_id not in str(item.get("content", ""))
            ]
    # ############# approval retry/rollback 清理结束 #############

    def _reset_from_subtask(self, state: Dict[str, Any], target_subtask_id: str):
        """Reset the target subtask and all subsequent ones to pending."""
        found = False
        
        for st in state.get("subtasks", []):
            print(st.get("subtask_id"))
            if st.get("subtask_id") == target_subtask_id:
                found = True
            if found:
                # ############# approval retry/rollback 清理开始 #############
                self._clear_reapproval_runtime_state(state, st.get("subtask_id"))
                # ############# approval retry/rollback 清理结束 #############
                st["status"] = "pending"
                st["retry_count"] = 0
                st["completion_criteria"] = {}
                st["evidence_summary"] = ""
                st["latest_state"] = {}
                st.pop("user_overrides", None)

    def _activate_subtask(self, state: Dict[str, Any], subtask_id: str, evidence_summary: str = ""):
        """Make the selected current subtask writable immediately after an intervention."""
        for st in state.get("subtasks", []):
            if st.get("subtask_id") == subtask_id:
                # ############# approval retry/rollback 清理开始 #############
                self._clear_reapproval_runtime_state(state, subtask_id)
                # ############# approval retry/rollback 清理结束 #############
                st["status"] = "in_progress"
                st["completion_criteria"] = {}
                st["latest_state"] = {}
                st["evidence_summary"] = evidence_summary
                st.pop("user_overrides", None)
                break