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
        return self.result


def task_state():
    return {
        "task_id": "T1",
        "description": "test task",
        "overall_status": "in_progress",
        "current_subtask": "S2",
        "subtasks": [
            {"subtask_id": "S1", "name": "one", "status": "completed"},
            {"subtask_id": "S2", "name": "two", "status": "in_progress"},
            {"subtask_id": "S3", "name": "three", "status": "pending"},
        ],
    }


class IntentRouterTest(unittest.TestCase):
    def route(self, result, **kwargs):
        llm = FakeLLM(result=result)
        route = IntentRouter(llm).route(
            user_message="message",
            task_state=kwargs.get("task_state", task_state()),
            pending_intervention=kwargs.get("pending_intervention"),
            global_mode=kwargs.get("global_mode", False),
            available_tasks=kwargs.get("available_tasks", []),
        )
        self.assertEqual(len(llm.calls), 1)
        return route

    def test_routes_task_query(self):
        route = self.route({
            "intent": "query", "query_scope": "task", "confidence": 0.96,
            "reason": "asks progress",
        })

        self.assertEqual(route["intent"], "query")
        self.assertEqual(route["target_task_id"], "T1")
        self.assertFalse(route["needs_clarification"])

    def test_routes_irrelevant_as_query_scope(self):
        route = self.route({
            "intent": "query", "query_scope": "irrelevant", "confidence": 0.93,
        })

        self.assertEqual(route["intent"], "query")
        self.assertEqual(route["query_scope"], "irrelevant")

    def test_normalizes_control_request_and_scalar_value(self):
        route = self.route({
            "intent": "confirm", "confirm_stage": "request", "confidence": 0.97,
            "action": {
                "action": "override_field", "subtask_id": "S2",
                "field": "distance_error_max", "value": "0.05",
            },
        })

        self.assertEqual(route["intent"], "confirm")
        self.assertEqual(route["confirm_stage"], "request")
        self.assertEqual(route["action"]["value"], 0.05)
        self.assertFalse(route["needs_clarification"])

    def test_router_does_not_apply_current_subtask_business_rule(self):
        route = self.route({
            "intent": "confirm", "confirm_stage": "request", "confidence": 0.91,
            "action": {"action": "force_complete", "subtask_id": "S3"},
        })

        self.assertEqual(route["action"], {"action": "force_complete", "subtask_id": "S3"})

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
        route = self.route({
            "intent": "confirm", "confirm_stage": "request", "confidence": 0.95,
            "action": {"action": "adjust_criterion_tolerance", "subtask_id": "S2"},
        })

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
