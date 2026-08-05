"""Generate user-facing replies from resolved routes and task facts."""

import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

import yaml
from .llm_client import LLMClient
from .prompts import REPLY_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class QueryResponder:
    def __init__(self, llm: LLMClient):
        self.llm = llm
        # ########## 修改内容：加载 criteria.yaml 中的判据解释，用于自然语言说明未满足判据含义。 ################
        self.criteria_config = self._load_criteria_config()
        # ################

    def answer_query(
        self,
        user_message: str,
        task_state: Dict[str, Any],
        pending_intervention: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Answer a task query after IntentRouter has selected the Query path."""
        operation_result = self._build_query_operation_result(task_state, user_message=user_message)
        reply_intent = (
            "根据用户问题和本轮任务事实回答。"
            "回答失败、卡点或处理建议时，先说明失败现象和完成条件未满足的含义；失败不等于机器人内部异常。"
            "用户未明确询问异常或内部故障时，不主动说明异常证据状态，也不要补充否定性异常说明。"
            "没有有效异常建议时，不得断言存在内部异常，只给出与当前失败点直接相关的排查建议。"
        )
        if operation_result.get("advice_source") == "anomaly_advisor":
            if self._wants_failure_anomaly_relation(user_message):
                reply_intent = (
                    "回答失败与异常之间的关系。先说明失败的直接事实，再说明当前存在的异常状态，"
                    "最后说明二者属于关联影响或排查线索，不得说成确定因果。"
                )
            elif self._wants_anomaly_details(user_message):
                reply_intent = (
                    "回答当前存在的异常。按照当前异常状态、异常含义、影响环节和建议检查项的顺序回答；"
                    "只说明本轮提供的异常，不得补充候选模块或未提供的状态。"
                )
            else:
                reply_intent = (
                    "回答失败、卡点或处理建议。默认只说明失败现象、流程影响和建议检查项；"
                    "不要主动展开异常证据或把异常说成确定失败原因。"
                )
        if pending_intervention:
            reply_intent = (
                "回答用户关于当前待确认控制动作的问题或影响。不得执行、替换或清除该动作；"
                "说明确认前任务流程不会变化。"
            )
            operation_result = {
                **operation_result,
                "pending_action": pending_intervention.get("action") or {},
                "requires_user_confirmation": True,
            }
        return self.generate_reply(
            reply_intent=reply_intent,
            user_message=user_message,
            task_state=task_state,
            operation_result=operation_result,
        )

    def answer_global_query(self, user_message: str, available_tasks: List[Dict[str, Any]]) -> str:
        """Answer a cross-task query without selecting a second intent path."""
        return self.generate_reply(
            reply_intent="根据可用任务列表回答全局任务查询；只使用本轮提供的任务事实。",
            user_message=user_message,
            task_state={
                "task_id": "global",
                "description": "全局任务视图",
                "overall_status": "global",
                "current_subtask": None,
                "subtasks": [],
            },
            operation_result={"global_tasks": available_tasks},
        )

    def answer_query_clarification(
        self,
        user_message: str,
        task_state: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> str:
        """Safely ask for a clearer message after routing fallback."""
        state = task_state or {
            "task_id": None,
            "overall_status": "unknown",
            "current_subtask": None,
            "subtasks": [],
        }
        return self.generate_reply(
            reply_intent=(
                "本轮无法可靠判断用户要查询的内容。请只请求用户补充任务 ID、查询主题或具体问题；"
                "不得推断为流程控制，也不得修改任务。"
            ),
            user_message=user_message,
            task_state=state,
            operation_result={"routing_clarification": reason or "unclear_query"},
            max_tokens=240,
        )

    def answer_irrelevant(
        self,
        user_message: str,
        task_state: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Decline an irrelevant Query while preserving the legacy response type."""
        state = task_state or {}
        return self.generate_reply(
            reply_intent=(
                "用户问题与任务无关。简短说明当前只能处理任务进度、失败原因、判据、异常建议或流程干预；"
                "不要展开无关内容或任务细节。"
            ),
            user_message=user_message,
            task_state={
                "task_id": state.get("task_id"),
                "overall_status": "hidden_for_irrelevant",
                "current_subtask": None,
                "subtasks": [],
            },
            operation_result={"irrelevant": True, "hide_task_details": True},
            max_tokens=160,
        )

    def answer_control_clarification(
        self,
        user_message: str,
        task_state: Dict[str, Any],
        reason: Optional[str] = None,
    ) -> str:
        """Ask for missing control fields without creating pending state."""
        return self.generate_reply(
            reply_intent="说明控制请求信息不完整，请用户补充动作、目标子任务、参数名或修改值。",
            user_message=user_message,
            task_state=task_state,
            operation_result={"error": reason or "incomplete_control_request"},
            max_tokens=300,
        )

    def generate_confirmation_request(
        self,
        user_message: str,
        action: Dict[str, Any],
        task_state: Dict[str, Any],
    ) -> str:
        """干预执行前，由 LLM 生成二次确认话术。"""
        return self.generate_reply(
            reply_intent="用户提出了流程干预。请先说清楚即将执行的修改、可能影响的步骤，并要求用户回复“确认”或“取消”后再执行",
            user_message=user_message,
            task_state=task_state,
            operation_result={"pending_action": action, "requires_user_confirmation": True},
            temperature=0.35,
            max_tokens=420,
        )

    def generate_intervention_response(
        self, user_message: str, intervention_result: Dict[str, Any], task_state: Dict[str, Any]
    ) -> str:
        """干预执行后，由 LLM 根据总体意图生成自然回复。"""
        ok = not bool(intervention_result.get("error"))
        if ok:
            reply_intent = "说明用户已确认，干预已执行成功，概括流程变化和当前下一步"
        else:
            reply_intent = "说明用户已确认，但干预未能执行，解释失败原因并给出可操作建议"
        return self.generate_reply(
            reply_intent=reply_intent,
            user_message=user_message,
            task_state=task_state,
            operation_result=intervention_result,
            temperature=0.35,
            max_tokens=420,
        )

    # ---------- 增强的生成回复（支持失败类型建议）----------
    def _build_structured_failure_advice(self, subtask_id: str, hard_unmet: List[str], soft_unmet: List[str]) -> str:
        """依据 Subtask Failure Policy 生成策略建议，返回详细、可操作的段落。"""
        failure_mapping = {
            "distance_error_max": "State Constraint Failure",
            "angle_error_max": "State Constraint Failure",
            "speed_stable_frames": "State Constraint Failure",
            "min_grid_count": "Perception Failure",
            "panel_visible_flag": "Perception Failure",
            "slot_pose_delta_max": "Perception Failure",
            "plug_pose_delta_max": "Perception Failure",
            "slot_stable_flag": "Perception Failure",
            "plug_stable_flag": "Perception Failure",
            "ik_valid_flag": "Planning Failure",
            "grasp_done_flag": "Execution Failure",
            "insert_done_flag": "Execution Failure",
            "visual_check_flag": "Verification Failure",
            "arm_reset_flag": "Execution Failure",
            "return_position_error_max": "State Constraint Failure",
        }
        all_unmet = hard_unmet + soft_unmet
        failure_types = set()
        for u in all_unmet:
            ft = failure_mapping.get(u, "Unknown")
            failure_types.add(ft)
        advice_map = {
            "Perception Failure": "增强观测信息质量（等待多帧稳定）、调整传感器视角或重新触发感知识别。建议检查视觉传感器的视野是否被遮挡，或者调整机器人位姿以获得更清晰的图像。",
            "State Constraint Failure": "微调运动控制参数（速度/姿态）、等待系统进入稳定状态、适当放宽阈值或检查环境扰动。如果环境扰动较大（如海流），可考虑降低运动速度或启用更精细的闭环控制。",
            "Planning Failure": "更换可行的末端目标配置、重新计算逆运动学解空间、检查工作空间与碰撞约束。可以尝试调整机械臂的初始位姿，或者选择其他可到达的夹取/插入点。",
            "Execution Failure": "重试当前动作、检查控制器通信状态、降低执行速度或校验执行器状态。请确认机械臂是否处于正常状态，液压/电力供应是否稳定。",
            "Verification Failure": "重新执行末端动作、重新进行视觉/传感器确认。建议在光线充足、相机视野清晰的情况下再次执行验证步骤。",
        }
        advices = [advice_map.get(ft, "请根据具体判据详情调整。") for ft in failure_types if ft in advice_map]
        if not advices:
            return "请检查具体判据阈值或通过干预指令（如“重试”、“回退”、“修改实际值”）进行处理。"
        return " ".join(advices)

    def generate_reply(
        self,
        reply_intent: str,
        user_message: str,
        task_state: Dict[str, Any],
        operation_result: Optional[Dict[str, Any]] = None,
        temperature: float = 0.35,
        max_tokens: int = 800,  # 增加 token 上限以支持更长回复
    ) -> str:
        """
        使用大模型生成最终回复。增加对未满足判据的自动建议注入，并支持结构化输出。
        """
        current_subtask_id = task_state.get("current_subtask")
        criterion_detail_requested = self._wants_criterion_details(user_message)
        failure_info = ""
        # ############# anomaly advice 接入开始 #############
        anomaly_advice_active = self._has_generated_anomaly_advice(operation_result)
        # ############# anomaly advice 接入结束 #############
        # ########## 修改内容：即使 anomaly_advice 生效，也注入判据解释作为背景依据。 ################
        if current_subtask_id:
            subtask = next((st for st in task_state.get("subtasks", []) if st.get("subtask_id") == current_subtask_id), None)
            if subtask and subtask.get("status") in ("in_progress", "failed", "waiting_approval"):
                criteria_result = subtask.get("completion_criteria", {})
                hard_unmet = criteria_result.get("hard_unmet_details", [])
                soft_unmet = criteria_result.get("soft_unmet_details", [])
                if hard_unmet or soft_unmet:
                    advice = "" if anomaly_advice_active else self._build_structured_failure_advice(current_subtask_id, hard_unmet, soft_unmet)
                    unmet_details = []
                    wants_details = criterion_detail_requested
                    explanations = self._criteria_explanations_for_subtask(subtask)
                    hard_details = criteria_result.get("hard_details", {})
                    for key in hard_unmet:
                        detail = hard_details.get(key, {})
                        expected = detail.get("expected", "?")
                        actual = detail.get("actual", "?")
                        if wants_details:
                            unmet_details.append(
                                self._format_unmet_criterion_detail(
                                    key=key,
                                    actual=actual,
                                    target_label="期望",
                                    target_value=expected,
                                    explanations=explanations,
                                    fallback_suffix="未达标",
                                )
                            )
                        else:
                            unmet_details.append(
                                self._format_unmet_criterion_meaning(
                                    key=key,
                                    explanations=explanations,
                                    fallback_suffix="对应硬判据尚未达标",
                                )
                            )
                    soft_details = criteria_result.get("soft_details", {})
                    for key in soft_unmet:
                        detail = soft_details.get(key, {})
                        required = detail.get("required", "?")
                        actual = detail.get("actual", "?")
                        if wants_details:
                            unmet_details.append(
                                self._format_unmet_criterion_detail(
                                    key=key,
                                    actual=actual,
                                    target_label="需要",
                                    target_value=required,
                                    explanations=explanations,
                                    fallback_suffix="未满足",
                                )
                            )
                        else:
                            unmet_details.append(
                                self._format_unmet_criterion_meaning(
                                    key=key,
                                    explanations=explanations,
                                    fallback_suffix="对应软判据尚未满足",
                                )
                            )
                    unmet_text = "\n".join(unmet_details) if unmet_details else "无"
                    criterion_block_name = "未满足判据详情" if wants_details else "未满足判据含义"
                    if anomaly_advice_active:
                        failure_info = (
                            f"\n【当前子任务 {current_subtask_id} {criterion_block_name}】\n{unmet_text}\n"
                        )
                        detail_policy = (
                            "用户明确询问判据详情，回复中可以包含判据字段、期望值/阈值和实际值。"
                            if wants_details else
                            "默认只用自然语言解释判据不满足代表的含义，不主动输出判据 key、期望值/阈值或实际值。"
                        )
                        reply_intent = (
                            f"{reply_intent}。如果用户询问卡点、失败原因、判据或处理建议，"
                            f"先用本轮提供的未满足条件说明直接失败原因。"
                            f"只有用户明确询问异常或失败与异常关系时，才结合本轮提供的异常说明解释异常链。"
                            f"异常与失败的关系只能表述为关联影响或排查线索，不得表述为确定原因，也不要反复使用可能、也许、大概。"
                            f"{detail_policy}"
                        )
                    else:
                        failure_info = (
                            f"\n【当前子任务 {current_subtask_id} {criterion_block_name}】\n{unmet_text}\n\n"
                            f"【故障诊断与处理策略】\n{advice}\n"
                        )
                        detail_policy = (
                            "需要包含所有未满足判据的 key、期望值/阈值和实际值。"
                            if wants_details else
                            "只需结合当前子任务状态，用自然语言说明未满足判据代表什么，不主动输出判据 key、期望值/阈值或实际值。"
                        )
                        reply_intent = (
                            f"{reply_intent}。如果用户询问卡点、失败原因、判据、异常或处理建议，"
                            f"先用以下未满足判据说明直接失败原因，再基于该失败点保守说明可能指向的机器人运行环节。"
                            f"没有有效异常建议时不得断言系统已检测到某类异常，也不要使用高严重度等确定性表述。"
                            f"{detail_policy}"
                        )
        # ################

        reply_context = self._build_reply_context(
            task_state=task_state,
            operation_result=operation_result or {},
            user_message=user_message,
            include_criterion_details=criterion_detail_requested,
            anomaly_advice_active=anomaly_advice_active,
        )
        operation_context = self._build_operation_context(operation_result or {}, task_state)
        system_prompt = REPLY_SYSTEM_PROMPT.format(
            task_state=json.dumps(reply_context, ensure_ascii=False, indent=2),
            operation_result=json.dumps(operation_context, ensure_ascii=False, indent=2),
            reply_intent=reply_intent,
            user_message=user_message,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        try:
            response = self.llm.generate(messages, temperature=temperature, max_tokens=max_tokens)
            response = self._filter_model_name(response).strip()
            if response:
                return response
            logger.warning("LLM generate_reply returned empty text")
        except Exception as e:
            logger.exception("LLM generate_reply failed: %s", e)
        return self._fallback_reply(reply_intent, user_message, task_state, operation_result)

    def _build_reply_context(
        self,
        task_state: Dict[str, Any],
        operation_result: Dict[str, Any],
        user_message: str,
        include_criterion_details: bool,
        anomaly_advice_active: bool,
    ) -> Dict[str, Any]:
        """Build a compact, user-facing context for the reply LLM."""
        current_subtask_id = task_state.get("current_subtask")
        current_subtask = self._find_subtask(task_state, current_subtask_id)
        context: Dict[str, Any] = {
            "任务概况": {
                "任务编号": task_state.get("task_id"),
                "任务说明": task_state.get("description"),
                "整体状态": self._status_label(task_state.get("overall_status")),
            },
            "当前推进位置": self._subtask_context(current_subtask, current_subtask_id),
            "流程概览": [
                self._subtask_progress_line(st)
                for st in task_state.get("subtasks", [])
            ],
        }

        failure_context = self._failure_context_for_reply(current_subtask, include_criterion_details)
        if failure_context:
            context["当前失败或卡点说明"] = failure_context

        if anomaly_advice_active:
            anomaly_context = self._anomaly_context_for_reply(
                (operation_result or {}).get("anomaly_advice_summary") or {},
                include_criterion_details=include_criterion_details,
            )
            if anomaly_context:
                context["当前异常说明"] = anomaly_context

        if (operation_result or {}).get("irrelevant"):
            context["本轮处理说明"] = "用户问题与当前任务无关，回复时不要展开任务细节。"

        return context

    def _build_operation_context(self, operation_result: Dict[str, Any], task_state: Dict[str, Any]) -> Dict[str, Any]:
        """Convert operation_result into a compact natural-language context."""
        if not isinstance(operation_result, dict) or not operation_result:
            return {}

        if operation_result.get("global_tasks") is not None:
            return {
                "处理类型": "全局任务查询",
                "可用任务": operation_result.get("global_tasks") or [],
            }

        if operation_result.get("requires_user_confirmation") or operation_result.get("pending_action"):
            action = operation_result.get("pending_action") or {}
            return {
                "处理类型": "等待用户确认的流程干预",
                "将执行": self._describe_action(action, task_state),
                "确认要求": "用户确认前不会修改任务流程；用户需要回复“确认”或“取消”。",
            }

        if operation_result.get("cancelled_pending_intervention"):
            return {
                "处理类型": "取消待确认干预",
                "处理结果": "待确认的流程干预已取消，任务状态保持不变。",
            }

        if operation_result.get("error"):
            return {
                "处理类型": "处理失败",
                "失败原因": operation_result.get("message") or operation_result.get("error"),
            }

        if operation_result.get("advice_source") == "anomaly_advisor":
            return {
                "处理类型": "任务查询",
                "处理结果": "本轮已整理当前失败说明、异常说明和排查建议；回复时按用户问题类型选择使用。",
            }

        action = operation_result.get("action")
        if action:
            target = operation_result.get("to_subtask") or operation_result.get("subtask_id")
            target_text = f"{target}{self._subtask_name(task_state, target)}" if target else "当前子任务"
            return {
                "处理类型": "已执行的流程干预",
                "执行动作": self._action_label(action),
                "作用目标": target_text,
                "执行结果": "成功" if operation_result.get("ok") or not operation_result.get("error") else "失败",
                "流程变化": self._intervention_change_text(operation_result, task_state),
            }

        return {"处理结果": "系统已完成本轮处理。"}

    def _find_subtask(self, task_state: Dict[str, Any], subtask_id: Optional[str]) -> Dict[str, Any]:
        for subtask in (task_state or {}).get("subtasks", []):
            if subtask.get("subtask_id") == subtask_id:
                return subtask
        return {}

    def _subtask_context(self, subtask: Dict[str, Any], fallback_id: Optional[str]) -> Dict[str, Any]:
        subtask_id = subtask.get("subtask_id") or fallback_id
        name = subtask.get("name")
        return {
            "子任务": self._format_subtask_label(subtask_id, name),
            "状态": self._status_label(subtask.get("status")),
            "重试次数": subtask.get("retry_count", 0),
            "证据摘要": subtask.get("evidence_summary") or self._evidence_summary_for_reply(subtask, include_details=False),
        }

    def _subtask_progress_line(self, subtask: Dict[str, Any]) -> str:
        subtask_id = subtask.get("subtask_id")
        name = subtask.get("name")
        status = self._status_label(subtask.get("status"))
        return f"{self._format_subtask_label(subtask_id, name)}：{status}"

    def _failure_context_for_reply(self, subtask: Dict[str, Any], include_criterion_details: bool) -> Dict[str, Any]:
        if not isinstance(subtask, dict) or not subtask:
            return {}
        criteria = subtask.get("completion_criteria") or {}
        hard_unmet = list(criteria.get("hard_unmet_details") or [])
        soft_unmet = list(criteria.get("soft_unmet_details") or [])
        if not hard_unmet and not soft_unmet:
            return {}

        explanations = self._criteria_explanations_for_subtask(subtask)
        meanings: List[str] = []
        details: List[str] = []
        for key in hard_unmet:
            meanings.append(self._format_unmet_criterion_meaning(key, explanations, "对应硬判据尚未达标").strip(" -"))
            detail = (criteria.get("hard_details") or {}).get(key, {})
            details.append(
                self._format_unmet_criterion_detail(
                    key=key,
                    actual=detail.get("actual", "?"),
                    target_label="期望",
                    target_value=detail.get("expected", "?"),
                    explanations=explanations,
                    fallback_suffix="未达标",
                ).strip(" -")
            )
        for key in soft_unmet:
            meanings.append(self._format_unmet_criterion_meaning(key, explanations, "对应软判据尚未满足").strip(" -"))
            detail = (criteria.get("soft_details") or {}).get(key, {})
            details.append(
                self._format_unmet_criterion_detail(
                    key=key,
                    actual=detail.get("actual", "?"),
                    target_label="需要",
                    target_value=detail.get("required", "?"),
                    explanations=explanations,
                    fallback_suffix="未满足",
                ).strip(" -")
            )

        context: Dict[str, Any] = {
            "失败点": f"{self._format_subtask_label(subtask.get('subtask_id'), subtask.get('name'))}未满足完成条件。",
            "未满足条件含义": meanings,
            "默认表达要求": "默认用自然语言解释，不主动输出字段、阈值、期望值或实际值。",
        }
        if include_criterion_details:
            context["判据技术详情"] = details
        return context

    def _anomaly_context_for_reply(
        self,
        summary: Dict[str, Any],
        include_criterion_details: bool,
    ) -> Dict[str, Any]:
        if not isinstance(summary, dict) or not summary:
            return {}

        failure = summary.get("failure_observation") or {}
        active = summary.get("active_anomalies") or []
        relation = summary.get("failure_anomaly_relation") or {}

        context: Dict[str, Any] = {}
        if isinstance(failure, dict) and failure:
            context["失败说明"] = {
                "当前失败点": failure.get("summary"),
                "完成条件未满足的含义": list(failure.get("failure_reactions") or []),
            }

        if isinstance(active, list) and active:
            context["已提供的异常方向"] = [
                {
                    "异常": item.get("name"),
                    "状态": item.get("state"),
                    "含义": item.get("meaning"),
                    "影响环节": item.get("impact"),
                    "建议检查项": list(item.get("suggested_checks") or []),
                }
                for item in active
                if isinstance(item, dict)
            ]

        if isinstance(relation, dict) and relation.get("relation") != "none":
            context["失败与异常关系"] = {
                "关系性质": "关联影响或排查线索，不是确定因果。",
                "说明": relation.get("reason"),
                "表达要求": "不要说成确定导致；减少模糊词，不夸大严重性。",
            }

        if include_criterion_details:
            if summary.get("failed_criteria"):
                context["判据字段详情"] = summary.get("failed_criteria")
            if summary.get("failure_reactions"):
                context["判据含义详情"] = summary.get("failure_reactions")

        if summary.get("filtered_anomalies"):
            context["被过滤的异常记录"] = summary.get("filtered_anomalies")

        return context

    def _status_label(self, status: Any) -> str:
        mapping = {
            "completed": "已完成",
            "in_progress": "执行中",
            "pending": "待执行",
            "failed": "失败",
            "waiting_approval": "等待人工审核",
            "cancelled": "已取消",
            "hidden_for_irrelevant": "已隐藏",
            None: "未知",
        }
        return mapping.get(status, str(status))

    def _action_label(self, action: Any) -> str:
        mapping = {
            "rollback": "回退",
            "retry": "重试",
            "change_parameter": "修改参数",
            "force_complete": "人工完成当前子任务",
            "override_field": "覆盖状态字段",
            "waiting_approval": "等待人工审核",
            "advance": "推进到下一步",
            "completed": "完成子任务",
            "still_incomplete": "仍未满足完成条件",
        }
        return mapping.get(action, str(action))

    def _format_subtask_label(self, subtask_id: Any, name: Any = None) -> str:
        if subtask_id and name:
            return f"子任务 {subtask_id}“{name}”"
        if subtask_id:
            return f"子任务 {subtask_id}"
        return "当前子任务"

    def _intervention_change_text(self, result: Dict[str, Any], task_state: Dict[str, Any]) -> str:
        action = result.get("action")
        if action == "rollback":
            target = result.get("to_subtask")
            return f"流程已从 {target}{self._subtask_name(task_state, target)} 重新开始，目标子任务及其后续步骤已重置。"
        if action == "retry":
            target = result.get("subtask_id") or task_state.get("current_subtask")
            return f"{target}{self._subtask_name(task_state, target)} 已重新进入执行中。"
        if action == "change_parameter":
            target = result.get("subtask_id") or task_state.get("current_subtask")
            return f"{target}{self._subtask_name(task_state, target)} 的参数已更新，并从该步骤重新执行。"
        if action == "override_field":
            return result.get("message") or "状态字段已按人工确认值更新，并已重新评估当前子任务。"
        if action == "force_complete":
            target = result.get("subtask_id") or task_state.get("current_subtask")
            return f"{target}{self._subtask_name(task_state, target)} 已按人工确认完成处理。"
        return "流程状态已按系统处理结果更新。"

    # ########## 修改内容：从 criteria.yaml 获取并格式化判据解释。 ################
    def _load_criteria_config(self) -> Dict[str, Any]:
        path = Path(__file__).resolve().parents[1] / "config" / "criteria.yaml"
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("Failed to load criteria explanations from %s: %s", path, exc)
            return {}

    def _criteria_explanations_for_subtask(self, subtask: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        criteria_ref = (subtask or {}).get("criteria_ref")
        criteria_def = self.criteria_config.get(criteria_ref) if criteria_ref else None
        explanations = (criteria_def or {}).get("explanations") if isinstance(criteria_def, dict) else None
        return explanations if isinstance(explanations, dict) else {}

    def _completion_criteria_for_reply(self, criteria: Dict[str, Any], include_details: bool) -> Dict[str, Any]:
        if not isinstance(criteria, dict):
            return {}

        hard_unmet = list(criteria.get("hard_unmet_details") or [])
        soft_unmet = list(criteria.get("soft_unmet_details") or [])
        if include_details:
            hard_details = criteria.get("hard_details") or {}
            soft_details = criteria.get("soft_details") or {}
            return {
                "hard_met": criteria.get("hard_met", criteria.get("hard_satisfied")),
                "soft_met": criteria.get("soft_met", criteria.get("soft_satisfied")),
                "require_approval": criteria.get("require_approval"),
                "all_met": criteria.get("all_met"),
                "hard_unmet_details": hard_unmet,
                "soft_unmet_details": soft_unmet,
                "hard_details": {key: hard_details.get(key) for key in hard_unmet if key in hard_details},
                "soft_details": {key: soft_details.get(key) for key in soft_unmet if key in soft_details},
            }

        return {
            "hard_met": criteria.get("hard_met", criteria.get("hard_satisfied")),
            "soft_met": criteria.get("soft_met", criteria.get("soft_satisfied")),
            "hard_unmet_count": len(hard_unmet),
            "soft_unmet_count": len(soft_unmet),
        }

    def _evidence_summary_for_reply(self, subtask: Dict[str, Any], include_details: bool) -> str:
        criteria = (subtask or {}).get("completion_criteria") or {}
        hard_unmet = criteria.get("hard_unmet_details") or []
        soft_unmet = criteria.get("soft_unmet_details") or []
        if hard_unmet or soft_unmet:
            if include_details:
                return "该子任务存在未满足判据，具体字段和值见本轮注入的未满足判据详情。"
            return "该子任务存在未满足判据，具体含义见本轮注入的未满足判据含义。"
        if criteria:
            return "该子任务未发现未满足判据。"
        return "该子任务暂无判据评估摘要。"

    def _wants_criterion_details(self, user_message: str) -> bool:
        detail_keywords = (
            "判据", "硬判据", "软判据", "key", "字段", "期望", "实际", "阈值",
            "数值", "详情", "未满足项", "不满足项", "criteria", "criterion",
        )
        return any(keyword in (user_message or "") for keyword in detail_keywords)

    def _format_unmet_criterion_detail(
        self,
        key: str,
        actual: Any,
        target_label: str,
        target_value: Any,
        explanations: Dict[str, Dict[str, Any]],
        fallback_suffix: str,
    ) -> str:
        explanation = explanations.get(key) or {}
        name = explanation.get("name")
        unmet_meaning = explanation.get("unmet_meaning")
        key_text = f"{key}（{name}）" if name else key
        value_text = f"{target_label} {target_value}，实际 {actual}"
        if unmet_meaning:
            return f"  - {key_text}: {value_text}。{unmet_meaning}"
        return f"  - {key_text}: {value_text}（{fallback_suffix}）"

    def _format_unmet_criterion_meaning(
        self,
        key: str,
        explanations: Dict[str, Dict[str, Any]],
        fallback_suffix: str,
    ) -> str:
        explanation = explanations.get(key) or {}
        unmet_meaning = explanation.get("unmet_meaning")
        name = explanation.get("name")
        if unmet_meaning:
            return f"  - {unmet_meaning}"
        if name:
            return f"  - {name}{fallback_suffix}"
        return f"  - {fallback_suffix}"
    # ################

    # ############# anomaly advice 接入开始 #############
    def _build_query_operation_result(self, task_state: Dict[str, Any], user_message: str = "") -> Dict[str, Any]:
        advice = task_state.get("latest_anomaly_advice") if isinstance(task_state, dict) else None
        if (
            isinstance(advice, dict)
            and advice.get("advice_generated")
            and self._is_current_anomaly_advice(task_state, advice)
        ):
            include_filtered = self._wants_filtered_anomaly_details(user_message)
            return {
                "advice_source": "anomaly_advisor",
                "anomaly_advice": self._sanitize_anomaly_advice_for_reply(advice, include_filtered=include_filtered),
                "anomaly_advice_summary": self._build_anomaly_advice_summary(
                    task_state,
                    advice,
                    include_filtered=include_filtered,
                    include_criterion_details=self._wants_criterion_details(user_message),
                ),
                "criteria_failure_policy": "fallback_only",
            }
        return {"advice_source": "criteria_failure_policy"}

    def _build_anomaly_advice_summary(
        self,
        task_state: Dict[str, Any],
        advice: Dict[str, Any],
        include_filtered: bool = False,
        include_criterion_details: bool = False,
    ) -> Dict[str, Any]:
        current_subtask_id = task_state.get("current_subtask") if isinstance(task_state, dict) else None
        current_subtask = next(
            (st for st in (task_state or {}).get("subtasks", []) if st.get("subtask_id") == current_subtask_id),
            {},
        )
        criteria = current_subtask.get("completion_criteria") or {}
        hard_unmet = list(criteria.get("hard_unmet_details") or [])
        soft_unmet = list(criteria.get("soft_unmet_details") or [])
        failed_criteria = hard_unmet + soft_unmet
        explanations = self._criteria_explanations_for_subtask(current_subtask)

        def _criterion_reaction(key: str) -> str:
            explanation = explanations.get(key) or {}
            if explanation.get("unmet_meaning"):
                return str(explanation.get("unmet_meaning"))
            name = explanation.get("name")
            if name:
                return f"{name}未满足"
            return f"{key} 未满足"

        def _naturalize_text(value: Any) -> str:
            text = str(value or "")
            replacements = {
                "anomaly_state.perception=abnormal": "识别与定位环节存在异常迹象",
                "anomaly_state.planning=abnormal": "路径规划环节存在异常迹象",
                "anomaly_state.execution=abnormal": "动作执行环节存在异常迹象",
                "anomaly_state.plant=abnormal": "操作对象或接触状态存在异常迹象",
                "anomaly_state.data_commun=abnormal": "数据通信与状态上报链路存在异常迹象",
                "perception": "识别与定位环节",
                "planning": "路径规划环节",
                "execution": "动作执行环节",
                "plant": "操作对象或接触状态",
                "data_commun": "数据通信与状态上报链路",
                "anomaly_state": "状态信息",
            }
            for raw, label in replacements.items():
                text = text.replace(raw, label)
            return text

        def _naturalize_list(values: Any) -> list[str]:
            if not isinstance(values, list):
                return []
            return [_naturalize_text(value) for value in values if value is not None]

        def _compact_failure_observation(observation: Any) -> Dict[str, Any]:
            subtask_id = current_subtask.get("subtask_id") or current_subtask_id
            subtask_name = current_subtask.get("name")
            reactions = [_criterion_reaction(key) for key in failed_criteria]
            summary_parts = []
            if subtask_id and subtask_name:
                summary_parts.append(f"子任务 {subtask_id}“{subtask_name}”当前未满足完成条件")
            elif subtask_id:
                summary_parts.append(f"子任务 {subtask_id} 当前未满足完成条件")
            else:
                summary_parts.append("当前子任务未满足完成条件")
            if reactions:
                summary_parts.append("；".join(reactions))
            compacted = {
                "subtask_id": subtask_id,
                "subtask_name": subtask_name,
                "subtask_status": current_subtask.get("status"),
                "summary": "，".join(part for part in summary_parts if part),
                "failure_reactions": reactions,
            }
            if include_criterion_details and isinstance(observation, dict):
                compacted["failed_criteria"] = list(observation.get("failed_criteria") or failed_criteria)
            return compacted

        def _compact_anomaly(item: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "category": item.get("exception_category"),
                "priority": item.get("priority"),
                "meaning": _naturalize_text(item.get("meaning") or item.get("stage_meaning")),
                "task_profile_role": _naturalize_text(item.get("task_profile_role")),
                "task_profile_relevance": _naturalize_text(item.get("task_profile_relevance")),
            }

        def _sanitize_check_text(value: Any) -> Optional[str]:
            text = _naturalize_text(value).strip()
            if not text:
                return None
            noisy_markers = (
                "候选系统",
                "不要平均展开",
                "回答时应使用",
                "柔和表述",
                "不要直接断定",
                "接口数据",
            )
            if any(marker in text for marker in noisy_markers):
                return None
            return text

        def _compact_selected_modules(modules: Any) -> list[Dict[str, Any]]:
            if not isinstance(modules, list):
                return []
            compacted = []
            seen: set[str] = set()
            for module in modules:
                if not isinstance(module, dict):
                    continue
                module_name = module.get("module_name")
                if not module_name or module_name in seen:
                    continue
                seen.add(str(module_name))
                checks = []
                for check in module.get("suggested_checks") or []:
                    clean = _sanitize_check_text(check)
                    if clean:
                        checks.append(clean)
                compacted.append({
                    "module_name": module_name,
                    "confidence": module.get("confidence"),
                    "related_anomaly_type": module.get("related_anomaly_type"),
                    "suggested_checks": checks,
                })
            return compacted

        def _compact_anomaly_evidence(evidence: Any) -> Dict[str, Any]:
            if not isinstance(evidence, dict):
                return {}
            return {
                "status": evidence.get("status"),
                "basis": _naturalize_list(evidence.get("basis")),
                "matched_anomaly_types": _naturalize_list(evidence.get("matched_anomaly_types")),
                "note": _naturalize_text(evidence.get("note")),
            }

        def _active_anomaly_name(module: Dict[str, Any], anomaly: Dict[str, Any]) -> str:
            module_name = module.get("module_name") if isinstance(module, dict) else None
            if module_name:
                return f"{module_name}异常"
            category = anomaly.get("exception_category") if isinstance(anomaly, dict) else None
            return str(category or "当前异常")

        def _build_active_anomalies(matched_items: list[Dict[str, Any]], modules: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
            if not matched_items:
                return []
            active = []
            used_names: set[str] = set()
            module_by_type = {
                str(module.get("related_anomaly_type")): module
                for module in modules
                if module.get("related_anomaly_type")
            }
            fallback_module = modules[0] if modules else {}
            for anomaly in matched_items:
                module = module_by_type.get(str(anomaly.get("anomaly_type"))) or fallback_module
                name = _active_anomaly_name(module, anomaly)
                if name in used_names:
                    continue
                used_names.add(name)
                active.append({
                    "name": name,
                    "state": "异常",
                    "meaning": _naturalize_text(anomaly.get("meaning") or anomaly.get("stage_meaning")),
                    "impact": _naturalize_text(anomaly.get("task_profile_role") or anomaly.get("stage_meaning")),
                    "suggested_checks": list(module.get("suggested_checks") or []),
                })
            return active

        def _build_failure_anomaly_relation(
            failure: Dict[str, Any],
            active: list[Dict[str, Any]],
        ) -> Dict[str, Any]:
            if not active:
                return {"relation": "none"}
            failure_text = failure.get("summary") or "当前子任务完成条件未满足"
            reasons = []
            for item in active:
                impact = str(item.get("impact") or item.get("meaning") or "").strip()
                if impact:
                    if impact.startswith("影响"):
                        reasons.append(f"{item.get('name')}{impact}")
                    else:
                        reasons.append(f"{item.get('name')}会影响{impact}")
            return {
                "relation": "influence_clue",
                "causality": "not_confirmed",
                "failure": failure_text,
                "active_anomalies": [item.get("name") for item in active if item.get("name")],
                "reason": "；".join(reasons),
                "wording_rule": "表述为关联影响或排查线索，不得说成确定导致；整段最多使用一次可能、也许、大概等模糊词。",
            }

        matched = [item for item in (advice.get("matched_anomalies") or []) if isinstance(item, dict)]
        primary = matched[0] if matched else advice
        selected_modules = _compact_selected_modules(advice.get("selected_fault_modules"))
        selected_module_names = [item.get("module_name") for item in selected_modules if item.get("module_name")]
        failure_observation = _compact_failure_observation(advice.get("failure_observation"))
        active_anomalies = _build_active_anomalies(matched, selected_modules)
        summary = {
            "selected_module_names": selected_module_names,
            "failure_observation": failure_observation,
            "active_anomalies": active_anomalies,
            "failure_anomaly_relation": _build_failure_anomaly_relation(failure_observation, active_anomalies),
        }
        if include_criterion_details:
            summary["failed_criteria"] = failed_criteria
            summary["failure_reactions"] = [_criterion_reaction(key) for key in failed_criteria]
        if include_filtered:
            summary["filtered_anomalies"] = self._compact_filtered_anomalies(advice.get("filtered_anomalies"))
        return summary

    def _sanitize_anomaly_advice_for_reply(self, advice: Dict[str, Any], include_filtered: bool = False) -> Dict[str, Any]:
        sanitized = {
            "advice_generated": bool((advice or {}).get("advice_generated")),
            "exception_category": (advice or {}).get("exception_category"),
            "confidence": (advice or {}).get("confidence"),
            "selected_fault_modules_present": bool((advice or {}).get("selected_fault_modules")),
            "note": "详细异常诊断请使用 anomaly_advice_summary；最终回复应自然表达，只引用已选择模块，不要暴露内部字段名或重新推断故障模块。",
        }
        if include_filtered:
            sanitized["filtered_anomalies"] = self._compact_filtered_anomalies((advice or {}).get("filtered_anomalies"))
        return sanitized

    def _compact_filtered_anomalies(self, filtered: Any) -> Dict[str, list[Dict[str, Any]]]:
        if not isinstance(filtered, dict):
            return {}
        compacted: Dict[str, list[Dict[str, Any]]] = {}
        for group, items in filtered.items():
            if not isinstance(items, list):
                continue
            compacted[str(group)] = [
                {
                    "type": item.get("type"),
                    "category": item.get("category"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                }
                for item in items
                if isinstance(item, dict)
            ]
        return compacted

    def _wants_filtered_anomaly_details(self, user_message: str) -> bool:
        text = (user_message or "").lower()
        keywords = (
            "过滤",
            "忽略",
            "没说",
            "没有说",
            "为什么不说",
            "为什么没",
            "不该有",
            "不应该存在",
            "原始 anomaly",
            "原始异常",
            "out_of_scope",
            "filtered",
            "normal",
            "unknown",
        )
        return any(keyword in text for keyword in keywords)

    def _wants_anomaly_details(self, user_message: str) -> bool:
        text = (user_message or "").lower()
        relation_words = ("关系", "导致", "造成", "引起", "关联", "有关", "影响", "是不是因为")
        if any(word in text for word in relation_words):
            return False
        keywords = (
            "异常",
            "故障",
            "故障模块",
            "内部问题",
            "系统问题",
            "哪里异常",
            "什么异常",
            "有哪些异常",
            "存在的异常",
        )
        return any(keyword in text for keyword in keywords)

    def _wants_failure_anomaly_relation(self, user_message: str) -> bool:
        text = (user_message or "").lower()
        has_failure = any(word in text for word in ("失败", "卡点", "没完成", "不满足", "原因", "为什么"))
        has_anomaly = any(word in text for word in ("异常", "故障", "内部问题", "系统问题"))
        has_relation = any(word in text for word in ("关系", "导致", "造成", "引起", "关联", "有关", "影响", "是不是因为"))
        return (has_failure and has_anomaly) or (has_anomaly and has_relation)

    def _is_current_anomaly_advice(self, task_state: Dict[str, Any], advice: Dict[str, Any]) -> bool:
        """Use anomaly advice only when it belongs to the current active subtask."""
        if not isinstance(task_state, dict) or not isinstance(advice, dict):
            return False

        current_subtask_id = task_state.get("current_subtask")
        if not current_subtask_id or task_state.get("overall_status") == "completed":
            return False

        current_subtask = next(
            (st for st in task_state.get("subtasks", []) if st.get("subtask_id") == current_subtask_id),
            None,
        )
        if isinstance(current_subtask, dict):
            current_status = current_subtask.get("status")
            current_criteria = current_subtask.get("completion_criteria") or {}
            if current_status == "completed":
                return False
            if current_status == "waiting_approval" and current_criteria.get("all_met") is True:
                return False

        context = task_state.get("latest_anomaly_context")
        if isinstance(context, dict):
            context_subtask = context.get("current_subtask") or {}
            context_subtask_id = context_subtask.get("subtask_id") or context_subtask.get("id")
            if context_subtask_id:
                return context_subtask_id == current_subtask_id

        matched_anomalies = advice.get("matched_anomalies") or []
        if isinstance(matched_anomalies, list):
            for item in matched_anomalies:
                if not isinstance(item, dict):
                    continue
                matched_rule = str(item.get("matched_rule") or "")
                if matched_rule.startswith(f"{current_subtask_id}."):
                    return True

        classification = advice.get("classification") or {}
        if isinstance(classification, dict):
            matched_rule = str(classification.get("matched_rule") or "")
            if matched_rule.startswith(f"{current_subtask_id}."):
                return True

        return False

    def _has_generated_anomaly_advice(self, operation_result: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(operation_result, dict):
            return False
        advice = operation_result.get("anomaly_advice")
        return isinstance(advice, dict) and bool(advice.get("advice_generated"))
    # ############# anomaly advice 接入结束 #############

    def _fallback_reply(
        self,
        reply_intent: str,
        user_message: str,
        task_state: Dict[str, Any],
        operation_result: Optional[Dict[str, Any]] = None,
    ) -> str:
        """LLM 不可用时的降级回复，保证接口稳定。"""
        current_subtask = task_state.get("current_subtask") or "未知步骤"
        current_name = self._subtask_name(task_state, current_subtask)
        overall_status = task_state.get("overall_status") or "unknown"
        operation_result = operation_result or {}

        if operation_result.get("requires_user_confirmation") or operation_result.get("pending_action"):
            action_text = self._describe_action(operation_result.get("pending_action") or {}, task_state)
            return f"已识别到流程干预：{action_text}。该操作尚未执行，可能影响当前步骤及后续流程，请回复“确认”或“取消”。"

        if operation_result.get("cancelled_pending_intervention"):
            return f"已取消待确认的流程干预，任务状态保持不变。当前处于 {overall_status}，当前子任务为 {current_subtask}{current_name}。"

        if operation_result.get("error"):
            error_msg = operation_result.get("message") or operation_result.get("error")
            return f"系统未能完成本次处理：{error_msg}。当前任务状态为 {overall_status}，当前子任务为 {current_subtask}{current_name}。"

        # ############# anomaly advice 接入开始 #############
        anomaly_advice = operation_result.get("anomaly_advice") if isinstance(operation_result, dict) else None
        if isinstance(anomaly_advice, dict) and anomaly_advice.get("advice_generated"):
            advice_text = anomaly_advice.get("advice_text")
            if advice_text:
                return advice_text
        # ############# anomaly advice 接入结束 #############

        if "无关" in reply_intent or "拒绝" in reply_intent:
            return "当前仅支持本任务的进度查询、流程干预和参数修改，请围绕当前任务继续提问。"

        if "补充" in reply_intent or "解析失败" in reply_intent:
            return f"已识别到干预意图，但信息还不完整。请明确说明动作类型以及目标子任务；当前子任务为 {current_subtask}{current_name}。"

        if "取消" in reply_intent:
            return f"本次干预已取消，任务没有被修改。当前任务状态为 {overall_status}，当前子任务为 {current_subtask}{current_name}。"

        if "已执行" in reply_intent or "执行成功" in reply_intent:
            return f"干预已处理。当前任务状态为 {overall_status}，当前子任务为 {current_subtask}{current_name}。如需继续，可查询下一步或继续调整流程。"

        if overall_status == "completed":
            return "当前任务已完成。如需复核，可查询各子任务判据和执行记录。"
        if overall_status == "failed":
            return f"当前任务已失败，停留在 {current_subtask}{current_name}。如需恢复，请使用回退、重试或修改实际值指令。"

        return f"当前任务状态为 {overall_status}，当前子任务为 {current_subtask}{current_name}。如需继续，我可以协助查询进度、说明判据，或执行回退/重试/修改实际值。"

    def _describe_action(self, action: Dict[str, Any], task_state: Dict[str, Any]) -> str:
        if not isinstance(action, dict):
            return "修改当前任务流程"
        action_type = action.get("action")
        if action_type == "rollback":
            target = action.get("to_subtask") or task_state.get("current_subtask")
            return f"回退到 {target}{self._subtask_name(task_state, target)}"
        if action_type == "retry":
            target = action.get("subtask_id") or task_state.get("current_subtask")
            return f"重试 {target}{self._subtask_name(task_state, target)}"
        if action_type == "force_complete":
            target = action.get("subtask_id") or task_state.get("current_subtask")
            return f"人工完成 {target}{self._subtask_name(task_state, target)}"
        if action_type == "change_parameter":
            target = action.get("subtask_id") or task_state.get("current_subtask")
            parameter = action.get("parameter")
            value = action.get("value")
            return f"将 {target}{self._subtask_name(task_state, target)} 的参数 {parameter} 修改为 {value}"
        if action_type == "override_field":
            target = action.get("subtask_id") or task_state.get("current_subtask")
            field = action.get("field")
            value = action.get("value")
            return f"将 {target}{self._subtask_name(task_state, target)} 的状态字段 {field} 覆盖为 {value}"
        return "修改当前任务流程"

    def _subtask_name(self, task_state: Dict[str, Any], subtask_id: Optional[str]) -> str:
        if not subtask_id:
            return ""
        for st in task_state.get("subtasks", []):
            if st.get("subtask_id") == subtask_id:
                name = st.get("name")
                return f"（{name}）" if name else ""
        return ""

    def _filter_model_name(self, text: str) -> str:
        banned = ["qwen", "Qwen", "通义千问", "vLLM", "阿里云", "通义", "大模型", "模型"]
        for word in banned:
            text = text.replace(word, "系统")
        return text
