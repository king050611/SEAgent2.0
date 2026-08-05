"""Single-entry LLM intent routing for task queries and flow control."""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .llm_client import LLMClient
from .task_intent_retriever import compact_task_intent_summary


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
  "query_topics": ["location", "equipment"],
  "confirm_stage": "request"、"decision"或null,
  "decision": "confirm"、"cancel"或null,
  "needs_clarification": true或false,
  "confidence": 0.0到1.0,
  "reason": "简短原因"
}}

路由规则：
1. Query 表示获取信息且不改变任务状态，包括任务编号/类型/优先级、时间、位置、水深、目标井口/经纬度/采油树/孔位、设备、环境条件、进度、状态、失败、判据、异常、下一步、已有控制动作的影响，以及无关问题。
2. 无关问题仍使用 intent=query，同时 query_scope=irrelevant。
3. Confirm 表示流程控制通道。提出新控制动作时 confirm_stage=request；对已有待确认动作作决定时 confirm_stage=decision。
4. 存在待确认动作时，询问该动作影响仍是 Query；明确“确认”或“取消”才是 Confirm/decision。
5. 第一阶段不得输出 action、to_subtask、subtask_id、parameter、field、value；控制动作参数由第二阶段动作编译器生成。
6. decision 只允许 confirm 或 cancel。没有明确决定时不要输出 decision 阶段。
7. 局部模式 target_task_id 使用当前任务 ID；全局模式在同一次判断中从可用任务提取目标。全局汇总查询可使用 query_scope=global 且 target_task_id=null。

query_topics 规则：
- “任务编号是什么、任务类型、优先级” → task_identity
- “什么时候开始/结束” → time
- “在哪个油田、水深多少” → location
- “目标经纬度、井口编号、采油树类型、孔位” → task_details
- “使用什么机器人、带什么载荷、支持船是什么” → equipment
- “任务有哪些环境条件/作业条件” → conditions
- “当前进行到哪一步、进度、状态” → runtime
- “为什么判据没满足、完成条件、阈值/实际值” → criteria
- “当前有什么异常、异常建议” → anomaly
- “这个待确认动作/回退/重试会有什么影响” → pending_control
- 用户一次询问多个主题时输出多个 query_topics，例如 ["location", "equipment", "runtime"]。
"""


ACTION_COMPILE_PROMPT = """\
你是水下任务监管系统的控制指令编译器。你的唯一任务是把用户原始自然语言编译成后端已经支持的精确 action JSON。

【用户原始消息】
{user_message}

【当前任务实体】
{task_entity}

【后端动作协议】
{backend_contracts}

输出要求：
1. 只输出一个 JSON 对象，不解释、不执行动作、不生成面向用户的回复。
2. 后端现有协议是唯一标准；必须使用协议中的精确字段名，不允许字段别名，不允许额外字段。
3. 如果用户信息足以生成控制动作，输出：
{{
  "status": "complete",
  "action": 后端协议中的精确 action 对象,
  "missing_fields": [],
  "reason": "简短原因"
}}
4. 如果用户信息确实不足，输出：
{{
  "status": "incomplete",
  "action": null,
  "missing_fields": ["缺少的后端字段名"],
  "reason": "缺少哪些信息"
}}
5. 有效子任务编号只能来自当前任务实体的 subtasks。
6. 不得输出 adjust_criterion_tolerance 或任何后端协议外动作。
"""


ACTION_REPAIR_PROMPT = """\
你是水下任务监管系统的控制指令编译器。你上一轮生成的动作不符合后端协议，必须根据校验错误重新生成。

【用户原始消息】
{user_message}

【当前任务实体】
{task_entity}

【后端动作协议】
{backend_contracts}

【上一轮模型输出】
{previous_result}

【后端校验错误】
{validation_errors}

修正要求：
1. 只输出一个 JSON 对象，不解释、不执行动作、不生成面向用户的回复。
2. 不得改变用户语义。
3. 必须使用后端协议中的精确字段名；不允许字段别名，不允许额外字段。
4. 如果用户信息足够，输出 status=complete 和符合协议的 action。
5. 只有用户信息确实缺失时，才输出 status=incomplete。
"""


BACKEND_ACTION_CONTRACTS = {
    "rollback": {
        "required_fields": ["action", "to_subtask"],
        "exact_structure": {"action": "rollback", "to_subtask": "<有效子任务编号>"},
    },
    "retry": {
        "required_fields": ["action", "subtask_id"],
        "exact_structure": {"action": "retry", "subtask_id": "<有效子任务编号>"},
    },
    "change_parameter": {
        "required_fields": ["action", "subtask_id", "parameter", "value"],
        "exact_structure": {
            "action": "change_parameter",
            "subtask_id": "<有效子任务编号>",
            "parameter": "<参数名称>",
            "value": "<参数值>",
        },
    },
    "force_complete": {
        "required_fields": ["action", "subtask_id"],
        "exact_structure": {"action": "force_complete", "subtask_id": "<有效子任务编号>"},
    },
    "override_field": {
        "required_fields": ["action", "subtask_id", "field", "value"],
        "exact_structure": {
            "action": "override_field",
            "subtask_id": "<有效子任务编号>",
            "field": "<状态字段名称>",
            "value": "<覆盖值>",
        },
    },
}


MAX_ACTION_COMPILE_ATTEMPTS = 2


class IntentRouter:
    VALID_INTENTS = {"query", "confirm"}
    VALID_QUERY_SCOPES = {"task", "global", "irrelevant"}
    VALID_QUERY_TOPICS = {
        "task_identity",
        "time",
        "location",
        "task_details",
        "equipment",
        "conditions",
        "runtime",
        "criteria",
        "anomaly",
        "pending_control",
    }
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
        route_result = self._classify_intent(
            user_message=user_message,
            task_state=task_state,
            pending_intervention=pending_intervention,
            global_mode=global_mode,
            available_tasks=tasks,
        )
        if route_result["intent"] == "query" or route_result.get("needs_clarification"):
            return route_result
        if route_result.get("confirm_stage") == "decision":
            return route_result

        action_task_state = task_state
        if global_mode and route_result.get("target_task_id"):
            action_task_state = next(
                (item for item in tasks if item.get("task_id") == route_result.get("target_task_id")),
                None,
            )
        compile_route = self._compile_route_action(user_message, action_task_state, route_result)
        return compile_route

    def _classify_intent(
        self,
        user_message: str,
        task_state: Optional[Dict[str, Any]],
        pending_intervention: Optional[Dict[str, Any]],
        global_mode: bool,
        available_tasks: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        prompt = ROUTE_PROMPT.format(
            user_message=user_message,
            mode="global" if global_mode else "task",
            task_state=json.dumps(self._task_summary(task_state), ensure_ascii=False, indent=2),
            pending_intervention=json.dumps(pending_intervention, ensure_ascii=False, indent=2),
            available_tasks=json.dumps(available_tasks, ensure_ascii=False, indent=2),
        )
        try:
            raw = self.llm.extract_json(
                [{"role": "user", "content": prompt}],
                max_tokens=800,
            )
        except Exception as error:
            logger.exception("Intent routing failed: %s", error)
            return self._fallback("意图路由模型调用失败")

        return self._normalize_route(raw, task_state, global_mode, available_tasks)

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
            "query_topics": [],
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
            result["query_topics"] = self._normalize_query_topics(raw.get("query_topics"), scope)
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

        return result

    def _normalize_query_topics(self, topics: Any, scope: Optional[str]) -> List[str]:
        if scope == "irrelevant":
            return []
        if isinstance(topics, str):
            topics = [topics]
        if not isinstance(topics, list):
            return ["runtime"] if scope in {"task", "global"} else []
        normalized: List[str] = []
        for topic in topics:
            if topic in self.VALID_QUERY_TOPICS and topic not in normalized:
                normalized.append(topic)
        return normalized or (["runtime"] if scope in {"task", "global"} else [])

    def _compile_route_action(
        self,
        user_message: str,
        task_state: Optional[Dict[str, Any]],
        route_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        compile_result = self._compile_backend_action(user_message, task_state)
        validation = self._validate_backend_action(compile_result, task_state)
        if validation["valid"]:
            return {
                **route_result,
                "action": validation["action"],
                "needs_clarification": False,
                "reason": str(compile_result.get("reason") or route_result.get("reason") or "已生成符合后端协议的控制动作"),
            }
        if validation.get("incomplete"):
            return self._build_missing_information_result(route_result, compile_result)

        last_errors = validation["errors"]
        for _ in range(MAX_ACTION_COMPILE_ATTEMPTS - 1):
            compile_result = self._repair_backend_action(
                user_message=user_message,
                task_state=task_state,
                previous_result=compile_result,
                validation_errors=last_errors,
            )
            validation = self._validate_backend_action(compile_result, task_state)
            if validation["valid"]:
                return {
                    **route_result,
                    "action": validation["action"],
                    "needs_clarification": False,
                    "reason": str(compile_result.get("reason") or "已按后端动作协议重新生成"),
                }
            if validation.get("incomplete"):
                return self._build_missing_information_result(route_result, compile_result)
            last_errors = validation["errors"]

        return self._build_generation_failure_result(route_result, last_errors)

    def _compile_backend_action(
        self,
        user_message: str,
        task_state: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        prompt = ACTION_COMPILE_PROMPT.format(
            user_message=user_message,
            task_entity=json.dumps(self._task_entity_context(task_state), ensure_ascii=False, indent=2),
            backend_contracts=json.dumps(BACKEND_ACTION_CONTRACTS, ensure_ascii=False, indent=2),
        )
        try:
            raw = self.llm.extract_json(
                [{"role": "user", "content": prompt}],
                max_tokens=700,
            )
        except Exception as error:
            logger.exception("Action compile failed: %s", error)
            return {
                "status": "generation_error",
                "action": None,
                "missing_fields": [],
                "reason": "动作编译模型调用失败",
            }
        return raw if isinstance(raw, dict) else {
            "status": "generation_error",
            "action": None,
            "missing_fields": [],
            "reason": "动作编译输出不是对象",
        }

    def _repair_backend_action(
        self,
        user_message: str,
        task_state: Optional[Dict[str, Any]],
        previous_result: Dict[str, Any],
        validation_errors: List[str],
    ) -> Dict[str, Any]:
        prompt = ACTION_REPAIR_PROMPT.format(
            user_message=user_message,
            task_entity=json.dumps(self._task_entity_context(task_state), ensure_ascii=False, indent=2),
            backend_contracts=json.dumps(BACKEND_ACTION_CONTRACTS, ensure_ascii=False, indent=2),
            previous_result=json.dumps(previous_result, ensure_ascii=False, indent=2),
            validation_errors=json.dumps(validation_errors, ensure_ascii=False, indent=2),
        )
        try:
            raw = self.llm.extract_json(
                [{"role": "user", "content": prompt}],
                max_tokens=700,
            )
        except Exception as error:
            logger.exception("Action repair failed: %s", error)
            return {
                "status": "generation_error",
                "action": None,
                "missing_fields": [],
                "reason": "动作修正模型调用失败",
            }
        return raw if isinstance(raw, dict) else {
            "status": "generation_error",
            "action": None,
            "missing_fields": [],
            "reason": "动作修正输出不是对象",
        }

    def _validate_backend_action(
        self,
        compile_result: Any,
        task_state: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        errors: List[str] = []
        if not isinstance(compile_result, dict):
            return {"valid": False, "incomplete": False, "action": None, "errors": ["动作编译输出必须是对象"]}

        status = compile_result.get("status")
        action = compile_result.get("action")
        missing_fields = compile_result.get("missing_fields")
        if status not in {"complete", "incomplete"}:
            errors.append("status 必须是 complete 或 incomplete")
        if action is not None and not isinstance(action, dict):
            errors.append("action 必须是对象或 null")
        if not isinstance(missing_fields, list):
            errors.append("missing_fields 必须是列表")

        if status == "incomplete":
            if action is not None:
                errors.append("status=incomplete 时 action 必须为 null")
            return {
                "valid": False,
                "incomplete": True,
                "action": None,
                "errors": errors,
            }

        if errors:
            return {"valid": False, "incomplete": False, "action": None, "errors": errors}
        return self._validate_action_object(action, task_state)

    def _validate_action_object(
        self,
        action: Any,
        task_state: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        errors: List[str] = []
        if not isinstance(action, dict):
            return {"valid": False, "incomplete": False, "action": None, "errors": ["status=complete 时 action 必须是对象"]}
        action_type = action.get("action")
        if action_type not in self.VALID_ACTIONS:
            return {
                "valid": False,
                "incomplete": False,
                "action": None,
                "errors": [f"不支持的动作 {action_type}"],
            }

        required_fields = set(BACKEND_ACTION_CONTRACTS[action_type]["required_fields"])
        actual_fields = set(action.keys())
        missing = required_fields - actual_fields
        extra = actual_fields - required_fields
        for field in sorted(missing):
            errors.append(f"{action_type} 缺少必填字段 {field}")
        for field in sorted(extra):
            errors.append(f"{action_type} 包含非法字段 {field}")

        valid_subtasks = {
            item.get("subtask_id") or item.get("id")
            for item in (task_state or {}).get("subtasks", [])
            if isinstance(item, dict)
        }

        if action_type == "rollback":
            target = action.get("to_subtask")
            if not isinstance(target, str) or not target:
                errors.append("rollback 的 to_subtask 必须是非空字符串")
            elif target not in valid_subtasks:
                errors.append(f"目标子任务不存在: {target}")
            if errors:
                return {"valid": False, "incomplete": False, "action": None, "errors": errors}
            return {"valid": True, "incomplete": False, "action": {"action": action_type, "to_subtask": target}, "errors": []}

        subtask_id = action.get("subtask_id")
        if not isinstance(subtask_id, str) or not subtask_id:
            errors.append(f"{action_type} 的 subtask_id 必须是非空字符串")
        elif subtask_id not in valid_subtasks:
            errors.append(f"目标子任务不存在: {subtask_id}")

        if action_type in {"retry", "force_complete"}:
            if errors:
                return {"valid": False, "incomplete": False, "action": None, "errors": errors}
            return {"valid": True, "incomplete": False, "action": {"action": action_type, "subtask_id": subtask_id}, "errors": []}

        if action_type == "change_parameter":
            parameter = action.get("parameter")
            if not isinstance(parameter, str) or not parameter:
                errors.append("change_parameter 的 parameter 必须是非空字符串")
            if action.get("value") is None:
                errors.append("change_parameter 的 value 不能为 null")
            if errors:
                return {"valid": False, "incomplete": False, "action": None, "errors": errors}
            return {
                "valid": True,
                "incomplete": False,
                "action": {
                    "action": action_type,
                    "subtask_id": subtask_id,
                    "parameter": parameter,
                    "value": self._coerce_scalar(action["value"]),
                },
                "errors": [],
            }

        field = action.get("field")
        if not isinstance(field, str) or not field:
            errors.append("override_field 的 field 必须是非空字符串")
        if action.get("value") is None:
            errors.append("override_field 的 value 不能为 null")
        if errors:
            return {"valid": False, "incomplete": False, "action": None, "errors": errors}
        return {
            "valid": True,
            "incomplete": False,
            "action": {
                "action": action_type,
                "subtask_id": subtask_id,
                "field": field,
                "value": self._coerce_scalar(action["value"]),
            },
            "errors": [],
        }

    @staticmethod
    def _build_missing_information_result(
        route_result: Dict[str, Any],
        compile_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        reason = str(compile_result.get("reason") or "")
        missing_fields = compile_result.get("missing_fields") or []
        if not reason:
            reason = f"控制请求信息不完整，缺少: {', '.join(str(item) for item in missing_fields)}"
        return {
            **route_result,
            "action": None,
            "needs_clarification": True,
            "reason": reason,
        }

    @staticmethod
    def _build_generation_failure_result(
        route_result: Dict[str, Any],
        validation_errors: List[str],
    ) -> Dict[str, Any]:
        return {
            **route_result,
            "action": None,
            "needs_clarification": True,
            "reason": f"action_compile_generation_failed: {'；'.join(validation_errors)}",
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
            "task_intent_summary": compact_task_intent_summary(state),
        }

    @staticmethod
    def _task_entity_context(task_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        state = task_state or {}
        return {
            "task_id": state.get("task_id"),
            "current_subtask": state.get("current_subtask"),
            "subtasks": [
                {
                    "subtask_id": item.get("subtask_id") or item.get("id"),
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
            "query_topics": ["runtime"],
            "confirm_stage": None,
            "decision": None,
            "action": None,
            "needs_clarification": True,
            "confidence": confidence,
            "reason": reason,
        }
