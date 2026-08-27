"""
state_monitor.py – 接收多源状态数据，更新子任务状态并触发判据评估。

关键约束：
1. 严格串行：只允许当前 current_subtask 接收状态上报。
2. 只允许 in_progress 子任务被评估，拒绝未来步骤和历史步骤误写。
3. 使用 StateStore.update_task_atomic 保证单任务文件内状态一致性。
4. 支持从整体 failed 状态恢复：若上报的子任务正是当前失败的子任务，允许重置并继续评估。
5. 当子任务进入 waiting_approval 时，自动生成审核通知存入任务状态，
   通知内容为三段式结构，并通过大模型生成或后备模板确保格式。
"""

from typing import Dict, Any
from .state_store import StateStore
from .criteria_evaluator import CriteriaEvaluator
import logging
import copy
import time

logger = logging.getLogger(__name__)


class StateMonitor:
    TERMINAL_STATUSES = {"completed", "failed", "waiting_approval"}

    def __init__(self, state_store: StateStore, criteria_evaluator: CriteriaEvaluator,
                 state_mapping: Dict, query_responder=None):
        self.state_store = state_store
        self.criteria_evaluator = criteria_evaluator
        self.state_mapping = state_mapping
        self.query_responder = query_responder   # 用于生成判据解释和通知

    def update_subtask_state(self, task_id: str, subtask_id: str, raw_state: Dict[str, Any]) -> Dict[str, Any]:
        """原子地更新当前子任务状态并评估判据。"""
        result: Dict[str, Any] = {"ok": True}

        def _update_func(state: Dict[str, Any]):
            nonlocal result

            overall = state.get("overall_status")
            if overall in ("completed", "failed"):
                # 如果任务 failed 但上报的是当前失败子任务，允许恢复
                if overall == "failed" and subtask_id == state.get("current_subtask"):
                    target = next((st for st in state.get("subtasks", []) if st.get("subtask_id") == subtask_id), None)
                    if target and target.get("status") in ("failed", "waiting_approval"):
                        # 重置子任务状态为 in_progress，整体状态改回 in_progress
                        target["status"] = "in_progress"
                        target["retry_count"] = 0
                        target["completion_criteria"] = {}
                        target["evidence_summary"] = ""
                        target["latest_state"] = {}
                        target.pop("user_overrides", None)
                        state["overall_status"] = "in_progress"
                        # 继续正常评估
                    else:
                        result = {
                            "error": "task_not_active",
                            "message": f"任务已处于 {overall} 状态，且当前子任务不可恢复。",
                        }
                        return
                else:
                    result = {
                        "error": "task_not_active",
                        "message": f"任务已处于 {overall} 状态，拒绝继续上报。",
                    }
                    return

            current_subtask = state.get("current_subtask")
            if subtask_id != current_subtask:
                result = {
                    "error": "subtask_out_of_order",
                    "message": f"流程必须严格串行。当前应执行 {current_subtask}，拒绝录入 {subtask_id} 的状态。",
                    "current_subtask": current_subtask,
                    "reported_subtask": subtask_id,
                }
                logger.warning(
                    "Task %s rejected out-of-order update: current=%s reported=%s",
                    task_id,
                    current_subtask,
                    subtask_id,
                )
                return

            subtasks = state.get("subtasks", [])
            target = next((st for st in subtasks if st.get("subtask_id") == subtask_id), None)
            if not target:
                result = {"error": f"subtask {subtask_id} not found"}
                return

            current_status = target.get("status")
            if current_status != "in_progress":
                result = {
                    "error": "subtask_not_writable",
                    "message": f"子任务 {subtask_id} 当前状态为 {current_status}，只有 in_progress 状态可接收上报。",
                    "subtask_id": subtask_id,
                    "status": current_status,
                }
                return

            mapped = self._map_state_fields(raw_state or {})
            target["latest_state"] = mapped

            # 合并用户覆盖值（如果有）
            overrides = target.get("user_overrides", {})
            merged_state = copy.deepcopy(mapped)
            merged_state.update(overrides)

            criteria_ref = target.get("criteria_ref")
            if criteria_ref:
                criteria_config = self.criteria_evaluator.get_criteria(criteria_ref)
                if criteria_config:
                    criteria_result = self.criteria_evaluator.evaluate(criteria_config, merged_state)
                    target["completion_criteria"] = criteria_result
                    target["evidence_summary"] = self._build_evidence_summary(criteria_result, merged_state, overrides)

                    all_met = criteria_result.get("all_met", False)
                    require_approval = criteria_result.get("require_approval", False)

                    if all_met:
                        if require_approval:
                            # 生成审核通知并存入状态
                            notification = self._generate_approval_notification(
                                task_id, subtask_id, criteria_result, state
                            )
                            if notification:
                                state.setdefault("notifications", []).append({
                                    "timestamp": time.time(),
                                    "content": notification,
                                    "read": False
                                })
                            target["status"] = "waiting_approval"
                            logger.info("Task %s subtask %s waiting approval", task_id, subtask_id)
                        else:
                            target["status"] = "completed"
                            logger.info("Task %s subtask %s completed", task_id, subtask_id)
                    else:
                        if not criteria_result.get("hard_met", False):
                            target["status"] = "failed"
                            unmet = criteria_result.get("hard_unmet_details", [])
                            logger.warning(
                                "Task %s subtask %s failed (hard criteria not met): %s",
                                task_id,
                                subtask_id,
                                unmet,
                            )
                        else:
                            target["status"] = "in_progress"
                            logger.debug(
                                "Task %s subtask %s hard met but soft not met, keep in_progress",
                                task_id,
                                subtask_id,
                            )
                else:
                    result = {"error": "criteria_not_found", "message": f"缺少判据配置 {criteria_ref}"}
                    logger.warning("Task %s subtask %s has no criteria config for %s", task_id, subtask_id, criteria_ref)
            else:
                target["status"] = "completed"
                target["evidence_summary"] = "未配置 criteria_ref，默认完成"
                logger.warning("Task %s subtask %s has no criteria_ref, auto completed", task_id, subtask_id)

        updated = self.state_store.update_task_atomic(task_id, _update_func)
        if updated is None and "error" not in result:
            result = {"error": "task not found"}
        return result

    def _generate_approval_notification(self, task_id: str, subtask_id: str,
                                        criteria_result: Dict, task_state: Dict) -> str:
        """
        生成需要人工审核的通知，强制三段式结构：
        1. 检测到软硬判据均已达标，列出具体指标和实际值；
        2. 当前子任务状态转变（in_progress -> waiting_approval）；
        3. 需要人工审核同意。
        优先使用大模型生成，失败时使用后备模板。
        """
        # 后备模板函数
        def fallback():
            return self._build_fallback_notification(subtask_id, task_state, criteria_result)

        if not self.query_responder:
            return fallback()

        # 获取子任务名称
        subtask_name = subtask_id
        for st in task_state.get("subtasks", []):
            if st.get("subtask_id") == subtask_id:
                subtask_name = st.get("name", subtask_id)
                break

        # 提取判据详情供大模型参考
        hard_details = criteria_result.get("hard_details", {})
        soft_details = criteria_result.get("soft_details", {})

        # 明确要求三段式输出
        reply_intent = (
            f"系统通知：子任务 {subtask_id}（{subtask_name}）已完成所有判据，需要人工审核。"
            f"请严格按照以下三段式结构输出通知（每段之间空一行）："
            f"第一段：说明检测到软硬判据均已达标，并逐一列出所有硬判据和软判据的具体指标和实际值（从提供的判据详情中提取）。"
            f"第二段：说明当前子任务状态从 'in_progress' 转变为 'waiting_approval'。"
            f"第三段：明确提示需要人工审核同意才能继续推进。"
            f"不要添加额外内容，只输出这三段。"
        )

        try:
            notification = self.query_responder.generate_reply(
                reply_intent=reply_intent,
                user_message="系统自动通知",
                task_state=task_state,
                operation_result={
                    "subtask_id": subtask_id,
                    "subtask_name": subtask_name,
                    "status": "waiting_approval",
                    "hard_details": hard_details,
                    "soft_details": soft_details,
                    "hard_met": criteria_result.get("hard_met"),
                    "soft_met": criteria_result.get("soft_met"),
                },
                temperature=0.3,
                max_tokens=450  # 足够容纳三段式内容
            )
            if notification:
                # 简单验证是否包含三个段落（至少有两个换行），若不满足则回退
                if notification.count('\n') >= 2:
                    return notification.strip()
                else:
                    logger.warning("Generated notification lacks three paragraphs, using fallback.")
                    return fallback()
        except Exception as e:
            logger.exception("生成审核通知失败: %s", e)

        return fallback()

    def _build_fallback_notification(self, subtask_id: str, task_state: Dict, criteria_result: Dict) -> str:
        """
        构建后备的三段式通知，不依赖大模型。
        """
        # 获取子任务名称
        subtask_name = subtask_id
        for st in task_state.get("subtasks", []):
            if st.get("subtask_id") == subtask_id:
                subtask_name = st.get("name", subtask_id)
                break

        # 第一段：列出判据达标详情
        hard_details = criteria_result.get("hard_details", {})
        soft_details = criteria_result.get("soft_details", {})
        lines = []
        lines.append("检测到软硬判据均已达标，具体如下：")
        if hard_details:
            lines.append("硬判据：")
            for key, detail in hard_details.items():
                expected = detail.get("expected", "?")
                actual = detail.get("actual", "?")
                lines.append(f"  - {key}: 期望 {expected}，实际 {actual}（达标）")
        if soft_details:
            lines.append("软判据：")
            for key, detail in soft_details.items():
                required = detail.get("required", "?")
                actual = detail.get("actual", "?")
                lines.append(f"  - {key}: 需要 {required}，实际 {actual}（满足）")
        first_part = "\n".join(lines)

        # 第二段：状态转变
        second_part = f"当前子任务 {subtask_id}（{subtask_name}）状态已从 'in_progress' 转变为 'waiting_approval'。"

        # 第三段：请求审核
        third_part = f"请人工审核同意后继续推进。您可以通过界面审核按钮或发送指令进行处理。"

        # 用空行分隔三段
        return f"{first_part}\n\n{second_part}\n\n{third_part}"

    def _map_state_fields(self, raw_state: Dict[str, Any]) -> Dict[str, Any]:
        mapped: Dict[str, Any] = {}
        mappings = self.state_mapping.get("mappings", {})
        for key, value in raw_state.items():
            mapped[mappings.get(key, key)] = value
        return mapped

    def _build_evidence_summary(self, criteria_result: Dict, merged_state: Dict, overrides: Dict = None) -> str:
        parts = []
        if criteria_result.get("all_met"):
            parts.append("所有判据满足")
            if overrides:
                parts.append(f"使用了用户覆盖值: {', '.join(overrides.keys())}")
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