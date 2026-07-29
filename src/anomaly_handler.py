"""
anomaly_handler.py – 处理子任务异常分支（重试、回退、人工审核、任务失败）
使用原子更新
"""

from typing import Dict, Any, Optional, List
from .state_store import StateStore
import logging

logger = logging.getLogger(__name__)


class AnomalyHandler:
    def __init__(self, task_templates: Dict[str, Any], state_store: StateStore):
        self.task_templates = task_templates
        self.state_store = state_store
        self.anomaly_actions = task_templates.get("anomaly_actions", {})

    def handle_anomaly(self, task_id: str, subtask_id: str, anomaly_key: str, unmet_criteria: List[str] = None) -> Dict[str, Any]:
        """原子地处理异常，返回动作结果"""
        result = {}

        def _handle_update(state):
            nonlocal result
            # 查找子任务模板中的异常映射
            subtask_tpl = None
            for st in self.task_templates.get("subtasks", []):
                if st["id"] == subtask_id:
                    subtask_tpl = st
                    break
            if not subtask_tpl:
                result = {"error": f"subtask template {subtask_id} not found"}
                return

            anomaly_action_key = subtask_tpl.get("anomalies", {}).get(anomaly_key)
            if not anomaly_action_key:
                anomaly_action_key = "manual_intervention"

            action_def = self.anomaly_actions.get(anomaly_action_key, {})
            action_type = action_def.get("action", "manual_intervention")
            message = action_def.get("message", "发生异常，需要处理")

            # 更新子任务状态
            subtasks = state["subtasks"]
            target = next((s for s in subtasks if s["subtask_id"] == subtask_id), None)
            if not target:
                result = {"error": "subtask not found"}
                return

            if action_type == "retry_subtask":
                result = self._blocked_auto_flow_action(
                    subtask_id=subtask_id,
                    action_type=action_type,
                    message=message,
                )
                return
                # 自动重试逻辑暂时关闭，保留旧代码痕迹如下：
                # retry_count = target.get("retry_count", 0) + 1
                # target["retry_count"] = retry_count
                # max_retries = subtask_tpl.get("max_retries", 2)
                # if retry_count <= max_retries:
                #     # ############# approval retry/rollback 清理开始 #############
                #     self._clear_reapproval_runtime_state(state, subtask_id)
                #     # ############# approval retry/rollback 清理结束 #############
                #     target["status"] = "in_progress"
                #     target["completion_criteria"] = {}
                #     target["evidence_summary"] = f"重试 {retry_count}/{max_retries}: {message}"
                #     target["latest_state"] = {}
                #     logger.info(f"Task {task_id} subtask {subtask_id} retry {retry_count}/{max_retries}")
                #     result = {"action": "retry", "subtask_id": subtask_id, "message": message}
                # else:
                #     # 超过重试次数，检查是否有回退目标
                #     if anomaly_action_key in ["rollback_to_S2", "rollback_to_S5", "retry_or_rollback_S3", "retry_or_rollback_S5"]:
                #         rollback_target = action_def.get("target", "S2")
                #         self._rollback_to(state, rollback_target)
                #         state["current_subtask"] = rollback_target
                #         self._activate_subtask(state, rollback_target)   # 自动激活目标子任务
                #         target["status"] = "failed"
                #         logger.warning(f"Task {task_id} retry exhausted, rollback to {rollback_target}")
                #         result = {"action": "rollback", "to_subtask": rollback_target, "message": f"重试失败，{message}"}
                #     else:
                #         target["status"] = "waiting_approval"
                #         logger.warning(f"Task {task_id} retry exhausted, waiting approval")
                #         result = {"action": "manual_intervention", "subtask_id": subtask_id, "message": f"重试失败，需要人工介入"}

            elif action_type == "rollback":
                target_sub = action_def.get("target", "S2")
                result = self._blocked_auto_flow_action(
                    subtask_id=subtask_id,
                    action_type=action_type,
                    message=message,
                    target_subtask=target_sub,
                )
                return
                # 自动回退逻辑暂时关闭，保留旧代码痕迹如下：
                # self._rollback_to(state, target_sub)
                # state["current_subtask"] = target_sub
                # self._activate_subtask(state, target_sub)   # 自动激活目标子任务
                # target["status"] = "failed"
                # logger.info(f"Task {task_id} rollback from {subtask_id} to {target_sub}")
                # result = {"action": "rollback", "to_subtask": target_sub, "message": message}

            elif action_type == "manual_intervention":
                target["status"] = "waiting_approval"
                target["evidence_summary"] += f"\n[需人工介入] {message}"
                logger.info(f"Task {task_id} subtask {subtask_id} requires manual intervention")
                result = {"action": "manual_intervention", "subtask_id": subtask_id, "message": message}

            elif action_type == "fail_task":
                target["status"] = "failed"
                state["overall_status"] = "failed"
                logger.error(f"Task {task_id} subtask {subtask_id} failed: {message}")
                result = {"action": "fail_task", "subtask_id": subtask_id, "message": message}

            elif action_type == "retry_or_rollback":
                target_sub = action_def.get("target", "S3")
                result = self._blocked_auto_flow_action(
                    subtask_id=subtask_id,
                    action_type=action_type,
                    message=message,
                    target_subtask=target_sub,
                )
                return
                # 自动重试/回退逻辑暂时关闭，保留旧代码痕迹如下：
                # retry_count = target.get("retry_count", 0) + 1
                # target["retry_count"] = retry_count
                # max_retries = subtask_tpl.get("max_retries", 2)
                # if retry_count <= max_retries:
                #     # ############# approval retry/rollback 清理开始 #############
                #     self._clear_reapproval_runtime_state(state, subtask_id)
                #     # ############# approval retry/rollback 清理结束 #############
                #     target["status"] = "in_progress"
                #     target["completion_criteria"] = {}
                #     target["evidence_summary"] = f"重试 {retry_count}/{max_retries}: {message}"
                #     target["latest_state"] = {}
                #     result = {"action": "retry", "subtask_id": subtask_id, "message": message}
                # else:
                #     target_sub = action_def.get("target", "S3")
                #     self._rollback_to(state, target_sub)
                #     state["current_subtask"] = target_sub
                #     self._activate_subtask(state, target_sub)   # 自动激活目标子任务
                #     target["status"] = "failed"
                #     logger.warning(f"Task {task_id} retry_or_rollback exhausted, rollback to {target_sub}")
                #     result = {"action": "rollback", "to_subtask": target_sub, "message": f"重试失败，{message}"}

            elif action_type == "rollback_or_manual":
                target_sub = action_def.get("target", "S5")
                result = self._blocked_auto_flow_action(
                    subtask_id=subtask_id,
                    action_type=action_type,
                    message=message,
                    target_subtask=target_sub,
                )
                return
                # 自动回退逻辑暂时关闭，保留旧代码痕迹如下：
                # self._rollback_to(state, target_sub)
                # state["current_subtask"] = target_sub
                # self._activate_subtask(state, target_sub)   # 自动激活目标子任务
                # target["status"] = "failed"
                # result = {"action": "rollback", "to_subtask": target_sub, "message": message}

            else:
                target["status"] = "waiting_approval"
                result = {"action": "manual_intervention", "subtask_id": subtask_id, "message": message}

        updated = self.state_store.update_task_atomic(task_id, _handle_update)
        if updated is None and "error" not in result:
            result = {"error": "task not found"}
        return result

    def _blocked_auto_flow_action(
        self,
        subtask_id: str,
        action_type: str,
        message: str,
        target_subtask: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return a manual-handling result when automatic retry/rollback is disabled."""
        return {
            "action": "auto_flow_action_blocked",
            "blocked_action": action_type,
            "subtask_id": subtask_id,
            "suggested_to_subtask": target_subtask,
            "message": f"{message}；自动重试/回退已关闭，请人工确认后再处理。",
        }

    # ############# approval retry/rollback 清理开始 #############
    def _clear_reapproval_runtime_state(self, task_state: Dict, subtask_id: str):
        """Invalidate stale approval/anomaly snapshots before retry or rollback reopens reporting."""
        task_state["anomaly_state"] = {}
        task_state["latest_anomaly_context"] = None
        task_state["latest_anomaly_advice"] = None
        notifications = task_state.get("notifications")
        if isinstance(notifications, list):
            task_state["notifications"] = [
                item for item in notifications
                if subtask_id not in str(item.get("content", ""))
            ]
    # ############# approval retry/rollback 清理结束 #############

    def _rollback_to(self, task_state: Dict, target_subtask_id: str):
        """将目标子任务及之后的所有子任务重置为 pending，并清除完成标记"""
        subtasks = task_state["subtasks"]
        found = False
        for st in subtasks:
            if st["subtask_id"] == target_subtask_id:
                found = True
            if found:
                # ############# approval retry/rollback 清理开始 #############
                self._clear_reapproval_runtime_state(task_state, st["subtask_id"])
                # ############# approval retry/rollback 清理结束 #############
                st["status"] = "pending"
                st["retry_count"] = 0
                st["completion_criteria"] = {}
                st["evidence_summary"] = ""
                st["latest_state"] = {}

    def _activate_subtask(self, task_state: Dict, subtask_id: str):
        """将指定子任务激活为 in_progress，清空历史状态，准备接收上报"""
        for st in task_state["subtasks"]:
            if st["subtask_id"] == subtask_id:
                # ############# approval retry/rollback 清理开始 #############
                self._clear_reapproval_runtime_state(task_state, subtask_id)
                # ############# approval retry/rollback 清理结束 #############
                st["status"] = "in_progress"
                st["retry_count"] = 0
                st["completion_criteria"] = {}
                st["evidence_summary"] = "异常回退后自动激活，重新执行"
                st["latest_state"] = {}
                logger.info(f"Activated subtask {subtask_id} after rollback")
                break