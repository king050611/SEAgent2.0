import unittest

from src.IntentRouter import IntentRouter


class FakeLLM:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def extract_json(self, messages, max_tokens=800):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if self.error:
            raise self.error
        if isinstance(self.result, list):
            index = len(self.calls) - 1
            return self.result[index]
        return self.result


def task_state():
    return {
        "task_id": "T1",
        "description": "test task",
        "overall_status": "in_progress",
        "current_subtask": "S2",
        "metadata": {
            "intent_id": "T1",
            "task_type": "valve_operation",
            "priority": "high",
            "location": {"oilfield": "A", "water_depth_m": 200.0},
            "equipment": {"robot_type": "work_class_rov"},
            "monitoring_runtime_example": {"current_stage": "S9"},
        },
        "subtasks": [
            {"subtask_id": "S1", "name": "one", "status": "completed"},
            {"subtask_id": "S2", "name": "two", "status": "in_progress"},
            {"subtask_id": "S3", "name": "three", "status": "pending"},
        ],
    }


class IntentRouterTest(unittest.TestCase):
    def route(self, result, expected_calls=1, **kwargs):
        llm = FakeLLM(result=result)
        route = IntentRouter(llm).route(
            user_message="message",
            task_state=kwargs.get("task_state", task_state()),
            pending_intervention=kwargs.get("pending_intervention"),
            global_mode=kwargs.get("global_mode", False),
            available_tasks=kwargs.get("available_tasks", []),
        )
        self.assertEqual(len(llm.calls), expected_calls)
        return route

    def test_routes_task_query(self):
        route = self.route({
            "intent": "query", "query_scope": "task", "confidence": 0.96,
            "reason": "asks progress",
        })

        self.assertEqual(route["intent"], "query")
        self.assertEqual(route["target_task_id"], "T1")
        self.assertFalse(route["needs_clarification"])
        self.assertEqual(route["query_topics"], ["runtime"])

    def test_routes_multiple_task_intent_query_topics(self):
        route = self.route({
            "intent": "query", "query_scope": "task", "confidence": 0.96,
            "query_topics": ["location", "equipment"],
            "reason": "asks location and equipment",
        })

        self.assertEqual(route["query_topics"], ["location", "equipment"])

    def test_task_summary_exposes_compact_task_intent_sections(self):
        llm = FakeLLM(result={
            "intent": "query", "query_scope": "task", "confidence": 0.96,
            "query_topics": ["location"],
        })
        IntentRouter(llm).route("水深多少", task_state())

        prompt = llm.calls[0]["messages"][0]["content"]
        self.assertIn('"task_intent_summary"', prompt)
        self.assertIn('"water_depth_m": 200.0', prompt)
        self.assertIn('"available_sections"', prompt)
        self.assertNotIn("monitoring_runtime_example", prompt)

    def test_routes_irrelevant_as_query_scope(self):
        route = self.route({
            "intent": "query", "query_scope": "irrelevant", "confidence": 0.93,
        })

        self.assertEqual(route["intent"], "query")
        self.assertEqual(route["query_scope"], "irrelevant")

    def test_normalizes_control_request_and_scalar_value(self):
        route = self.route([
            {"intent": "confirm", "confirm_stage": "request", "confidence": 0.97},
            {
                "status": "complete",
                "action": {
                    "action": "override_field", "subtask_id": "S2",
                    "field": "distance_error_max", "value": "0.05",
                },
                "missing_fields": [],
            },
        ], expected_calls=2)

        self.assertEqual(route["intent"], "confirm")
        self.assertEqual(route["confirm_stage"], "request")
        self.assertEqual(route["action"]["value"], 0.05)
        self.assertFalse(route["needs_clarification"])

    def test_router_does_not_apply_current_subtask_business_rule(self):
        route = self.route([
            {"intent": "confirm", "confirm_stage": "request", "confidence": 0.91},
            {
                "status": "complete",
                "action": {"action": "force_complete", "subtask_id": "S3"},
                "missing_fields": [],
            },
        ], expected_calls=2)

        self.assertEqual(route["action"], {"action": "force_complete", "subtask_id": "S3"})

    def test_control_request_compiles_backend_action_in_second_llm_call(self):
        llm = FakeLLM(result=[
            {"intent": "confirm", "confirm_stage": "request", "confidence": 0.97},
            {
                "status": "complete",
                "action": {"action": "rollback", "to_subtask": "S1"},
                "missing_fields": [],
                "reason": "rollback to S1",
            },
        ])

        route = IntentRouter(llm).route("回退到S1", task_state())

        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(route["action"], {"action": "rollback", "to_subtask": "S1"})
        self.assertIn("控制指令编译器", llm.calls[1]["messages"][0]["content"])
        self.assertIn('"to_subtask"', llm.calls[1]["messages"][0]["content"])

    def test_invalid_backend_fields_are_repaired_by_llm_not_converted_by_code(self):
        llm = FakeLLM(result=[
            {"intent": "confirm", "confirm_stage": "request", "confidence": 0.97},
            {
                "status": "complete",
                "action": {"action": "rollback", "subtask_id": "S1"},
                "missing_fields": [],
            },
            {
                "status": "complete",
                "action": {"action": "rollback", "to_subtask": "S1"},
                "missing_fields": [],
            },
        ])

        route = IntentRouter(llm).route("回退到S1", task_state())

        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(route["action"], {"action": "rollback", "to_subtask": "S1"})
        repair_prompt = llm.calls[2]["messages"][0]["content"]
        self.assertIn("缺少必填字段 to_subtask", repair_prompt)
        self.assertIn("包含非法字段 subtask_id", repair_prompt)

    def test_incomplete_control_request_reports_missing_fields(self):
        route = self.route([
            {"intent": "confirm", "confirm_stage": "request", "confidence": 0.95},
            {
                "status": "incomplete",
                "action": None,
                "missing_fields": ["subtask_id", "parameter", "value"],
                "reason": "缺少目标子任务、参数名称和参数值",
            },
        ], expected_calls=2)

        self.assertIsNone(route["action"])
        self.assertTrue(route["needs_clarification"])
        self.assertIn("缺少目标子任务、参数名称和参数值", route["reason"])

    def test_repeated_action_generation_failure_is_not_user_missing_information(self):
        route = self.route([
            {"intent": "confirm", "confirm_stage": "request", "confidence": 0.95},
            {
                "status": "complete",
                "action": {"action": "rollback", "subtask_id": "S1"},
                "missing_fields": [],
            },
            {
                "status": "complete",
                "action": {"action": "rollback", "subtask_id": "S1"},
                "missing_fields": [],
            },
        ], expected_calls=3)

        self.assertIsNone(route["action"])
        self.assertTrue(route["needs_clarification"])
        self.assertIn("action_compile_generation_failed", route["reason"])

    def test_routes_pending_confirmation_decision(self):
        route = self.route(
            {"intent": "confirm", "confirm_stage": "decision", "decision": "confirm", "confidence": 0.99},
            pending_intervention={"action": {"action": "retry", "subtask_id": "S2"}},
        )

        self.assertEqual(route["decision"], "confirm")
        self.assertIsNone(route["action"])

    def test_routes_pending_cancellation_decision(self):
        route = self.route(
            {"intent": "confirm", "confirm_stage": "decision", "decision": "cancel", "confidence": 0.99},
            pending_intervention={"action": {"action": "retry", "subtask_id": "S2"}},
        )

        self.assertEqual(route["decision"], "cancel")

    def test_global_mode_extracts_target_in_same_call(self):
        route = self.route(
            {"intent": "query", "target_task_id": "T2", "query_scope": "task", "confidence": 0.94},
            task_state=None,
            global_mode=True,
            available_tasks=[{"task_id": "T1"}, {"task_id": "T2"}],
        )

        self.assertEqual(route["target_task_id"], "T2")

    def test_global_summary_query_does_not_require_target(self):
        route = self.route(
            {"intent": "query", "target_task_id": None, "query_scope": "global", "confidence": 0.9},
            task_state=None,
            global_mode=True,
            available_tasks=[{"task_id": "T1"}, {"task_id": "T2"}],
        )

        self.assertEqual(route["query_scope"], "global")
        self.assertFalse(route["needs_clarification"])

    def test_unsupported_action_requires_clarification(self):
        route = self.route([
            {"intent": "confirm", "confirm_stage": "request", "confidence": 0.95},
            {
                "status": "complete",
                "action": {"action": "adjust_criterion_tolerance", "subtask_id": "S2"},
                "missing_fields": [],
            },
            {
                "status": "complete",
                "action": {"action": "adjust_criterion_tolerance", "subtask_id": "S2"},
                "missing_fields": [],
            },
        ], expected_calls=3)

        self.assertIsNone(route["action"])
        self.assertTrue(route["needs_clarification"])

    def test_llm_failure_falls_back_to_safe_query(self):
        llm = FakeLLM(error=RuntimeError("offline"))
        route = IntentRouter(llm).route("message", task_state())

        self.assertEqual(route["intent"], "query")
        self.assertTrue(route["needs_clarification"])

    def test_low_confidence_falls_back_to_safe_query(self):
        route = self.route({"intent": "confirm", "confidence": 0.2})

        self.assertEqual(route["intent"], "query")
        self.assertTrue(route["needs_clarification"])


if __name__ == "__main__":
    unittest.main()
