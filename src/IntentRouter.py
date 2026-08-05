"""Single-entry LLM intent routing for task queries and flow control."""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .llm_client import LLMClient


logger = logging.getLogger(__name__)


ROUTE_PROMPT = """\
你是水下任务监管系统唯一的意图路由器。只判断用户想做什么，不执行任务，也不生成面向用户的回复。

【用户消息】
{user_message}

【当前模式】
{mode}

【当前任务】
{task_state}

【当前待确认动作】
{pending_intervention}

【全局模式可用任务】
{available_tasks}

只输出一个 JSON 对象，字段固定为：
{{
  "intent": "query" 或 "confirm",
  "target_task_id": "任务ID或null",
  "query_scope": "task"、"global"、"irrelevant"或null,
  "confirm_stage": "request"、"decision"或null,
  "decision": "confirm"、"cancel"或null,
  "action": 对象或null,
  "needs_clarification": true或false,
  "confidence": 0.0到1.0,
  "reason": "简短原因"
}}

路由规则：
1. Query 表示获取信息且不改变任务状态，包括进度、状态、失败、判据、异常、下一步、已有控制动作的影响，以及无关问题。
2. 无关问题仍使用 intent=query，同时 query_scope=irrelevant。
3. Confirm 表示流程控制通道。提出新控制动作时 confirm_stage=request；对已有待确认动作作决定时 confirm_stage=decision。
4. 存在待确认动作时，询问该动作影响仍是 Query；明确“确认”或“取消”才是 Confirm/decision。
5. request 的 action 只允许以下五种结构：
   - {{"action":"rollback","to_subtask":"S2"}}
   - {{"action":"retry","subtask_id":"S2"}}
   - {{"action":"change_parameter","subtask_id":"S2","parameter":"hole_id","value":"port_3"}}
   - {{"action":"force_complete","subtask_id":"S2"}}
   - {{"action":"override_field","subtask_id":"S2","field":"distance_error_max","value":0.05}}
6. 动作缺少必要字段时 action=null、needs_clarification=true，不得猜测目标或值。
7. decision 只允许 confirm 或 cancel。没有明确决定时不要输出 decision 阶段。
8. 局部模式 target_task_id 使用当前任务 ID；全局模式在同一次判断中从可用任务提取目标。全局汇总查询可使用 query_scope=global 且 target_task_id=null。
9. 不得输出 adjust_criterion_tolerance 或任何未列出的动作。
"""


class IntentRouter:
    VALID_INTENTS = {"query", "confirm"}
    VALID_QUERY_SCOPES = {"task", "global", "irrelevant"}
    VALID_CONFIRM_STAGES = {"request", "decision"}
    VALID_CONFIRM_DECISIONS = {"confirm", "cancel"}
    VALID_ACTIONS = {
        "rollback",
        "retry",
        "change_parameter",
        "force_complete",
        "override_field",
    }

    def __init__(self, llm: LLMClient, confidence_threshold: float = 0.5):
        self.llm = llm
        self.confidence_threshold = confidence_threshold

    def route(
        self,
        user_message: str,
        task_state: Optional[Dict[str, Any]] = None,
        pending_intervention: Optional[Dict[str, Any]] = None,
        global_mode: bool = False,
        available_tasks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        tasks = available_tasks or []
        prompt = ROUTE_PROMPT.format(
            user_message=user_message,
            mode="global" if global_mode else "task",
            task_state=json.dumps(self._task_summary(task_state), ensure_ascii=False, indent=2),
            pending_intervention=json.dumps(pending_intervention, ensure_ascii=False, indent=2),
            available_tasks=json.dumps(tasks, ensure_ascii=False, indent=2),
        )
        try:
            raw = self.llm.extract_json(
                [{"role": "user", "content": prompt}],
                max_tokens=800,
            )
        except Exception as error:
            logger.exception("Intent routing failed: %s", error)
            return self._fallback("意图路由模型调用失败")

        return self._normalize_route(raw, task_state, global_mode, tasks)

    def _normalize_route(
        self,
        raw: Any,
        task_state: Optional[Dict[str, Any]],
        global_mode: bool,
        available_tasks: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not isinstance(raw, dict) or raw.get("intent") not in self.VALID_INTENTS:
            return self._fallback("意图路由输出无效")

        confidence = self._confidence(raw.get("confidence"))
        if confidence < self.confidence_threshold:
            return self._fallback("意图路由置信度不足", confidence)

        valid_task_ids = {
            item.get("task_id") for item in available_tasks
            if isinstance(item, dict) and item.get("task_id")
        }
        if global_mode:
            target_task_id = raw.get("target_task_id")
            if target_task_id not in valid_task_ids:
                target_task_id = None
        else:
            target_task_id = (task_state or {}).get("task_id")

        result = {
            "intent": raw["intent"],
            "target_task_id": target_task_id,
            "query_scope": None,
            "confirm_stage": None,
            "decision": None,
            "action": None,
            "needs_clarification": bool(raw.get("needs_clarification", False)),
            "confidence": confidence,
            "reason": str(raw.get("reason") or ""),
        }

        if result["intent"] == "query":
            scope = raw.get("query_scope")
            if scope not in self.VALID_QUERY_SCOPES:
                scope = "global" if global_mode and target_task_id is None else "task"
            result["query_scope"] = scope
            if global_mode and scope == "task" and target_task_id is None:
                result["needs_clarification"] = True
                result["reason"] = result["reason"] or "全局模式下缺少有效任务 ID"
            return result

        stage = raw.get("confirm_stage")
        if stage not in self.VALID_CONFIRM_STAGES:
            result["needs_clarification"] = True
            result["reason"] = result["reason"] or "缺少有效的控制阶段"
            return result

        result["confirm_stage"] = stage
        if global_mode and target_task_id is None:
            result["needs_clarification"] = True
            result["reason"] = result["reason"] or "全局模式下控制请求必须指定任务 ID"

        if stage == "decision":
            decision = raw.get("decision")
            if decision in self.VALID_CONFIRM_DECISIONS:
                result["decision"] = decision
            else:
                result["needs_clarification"] = True
                result["reason"] = result["reason"] or "没有明确确认或取消"
            return result

        action_task_state = task_state
        if global_mode and target_task_id:
            action_task_state = next(
                (item for item in available_tasks if item.get("task_id") == target_task_id),
                None,
            )
        result["action"] = self._normalize_action(raw.get("action"), action_task_state)
        if result["action"] is None:
            result["needs_clarification"] = True
            result["reason"] = result["reason"] or "控制动作缺少必要字段或包含无效目标"
        return result

    def _normalize_action(
        self,
        action: Any,
        task_state: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(action, dict):
            return None
        action_type = action.get("action")
        if action_type not in self.VALID_ACTIONS:
            return None

        valid_subtasks = {
            item.get("subtask_id") or item.get("id")
            for item in (task_state or {}).get("subtasks", [])
            if isinstance(item, dict)
        }

        if action_type == "rollback":
            target = action.get("to_subtask")
            if not target or target not in valid_subtasks:
                return None
            return {"action": action_type, "to_subtask": target}

        subtask_id = action.get("subtask_id")
        if not subtask_id or subtask_id not in valid_subtasks:
            return None

        if action_type in {"retry", "force_complete"}:
            return {"action": action_type, "subtask_id": subtask_id}

        if action_type == "change_parameter":
            parameter = action.get("parameter")
            if not parameter or action.get("value") is None:
                return None
            return {
                "action": action_type,
                "subtask_id": subtask_id,
                "parameter": parameter,
                "value": self._coerce_scalar(action["value"]),
            }

        field = action.get("field")
        if not field or action.get("value") is None:
            return None
        return {
            "action": action_type,
            "subtask_id": subtask_id,
            "field": field,
            "value": self._coerce_scalar(action["value"]),
        }

    @staticmethod
    def _coerce_scalar(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        lowered = stripped.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if re.fullmatch(r"[-+]?\d+", stripped):
            return int(stripped)
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", stripped):
            return float(stripped)
        return value

    @staticmethod
    def _confidence(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _task_summary(task_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        state = task_state or {}
        return {
            "task_id": state.get("task_id"),
            "description": state.get("description"),
            "overall_status": state.get("overall_status"),
            "current_subtask": state.get("current_subtask"),
            "subtasks": [
                {
                    "id": item.get("subtask_id") or item.get("id"),
                    "name": item.get("name"),
                    "status": item.get("status"),
                }
                for item in state.get("subtasks", [])
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _fallback(reason: str, confidence: float = 0.0) -> Dict[str, Any]:
        return {
            "intent": "query",
            "target_task_id": None,
            "query_scope": "task",
            "confirm_stage": None,
            "decision": None,
            "action": None,
            "needs_clarification": True,
            "confidence": confidence,
            "reason": reason,
        }
