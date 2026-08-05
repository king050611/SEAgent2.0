import unittest

from src.server import create_app


class FakeRouter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def route(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


class FakeResponder:
    def __init__(self):
        self.query_clarification_calls = []
        self.query_calls = []

    def answer_query(self, user_message, task_state, pending_intervention=None, query_topics=None):
        self.query_calls.append({
            "user_message": user_message,
            "task_state": task_state,
            "pending_intervention": pending_intervention,
            "query_topics": query_topics,
        })
        return "query answer"

    def answer_global_query(self, user_message, available_tasks):
        return "global answer"

    def answer_irrelevant(self, user_message, task_state=None):
        return "irrelevant answer"

    def answer_control_clarification(self, user_message, task_state, reason=None):
        return "clarify control"

    def answer_query_clarification(self, user_message, task_state, reason=None):
        self.query_clarification_calls.append((user_message, reason))
        return "clarify query"

    def generate_confirmation_request(self, user_message, action, task_state):
        return "confirm request"

    def generate_intervention_response(self, user_message, intervention_result, task_state):
        return "intervention result"

    def generate_reply(self, **kwargs):
        return "generated reply"


class FakeTaskManager:
    def __init__(self, pending=None):
        self.state = {
            "task_id": "T1", "description": "task", "overall_status": "in_progress",
            "current_subtask": "S1",
            "metadata": {
                "intent_id": "T1",
                "location": {"water_depth_m": 200.0},
            },
            "subtasks": [{"subtask_id": "S1", "name": "one", "status": "in_progress"}],
        }
        if pending:
            self.state["pending_intervention"] = pending
        self.set_calls = []
        self.clear_calls = []
        self.execute_calls = []

    def get_task_status(self, task_id):
        return dict(self.state) if task_id == "T1" else None

    def get_pending_intervention(self, task_state):
        return task_state.get("pending_intervention")

    def set_pending_intervention(self, **kwargs):
        self.set_calls.append(kwargs)
        return {"ok": True, "pending_intervention": {"action": kwargs["action"]}}

    def clear_pending_intervention(self, task_id):
        self.clear_calls.append(task_id)
        return {"ok": True, "pending_intervention": self.state.get("pending_intervention")}

    def execute_intervention(self, task_id, action):
        self.execute_calls.append((task_id, action))
        return {"ok": True, **action}


class FakeStateStore:
    def __init__(self, manager):
        self.manager = manager

    def list_tasks(self):
        return {"T1": dict(self.manager.state)}


def build_client(route, pending=None):
    router = FakeRouter(route)
    manager = FakeTaskManager(pending=pending)
    responder = FakeResponder()
    app = create_app(
        task_manager=manager,
        query_responder=responder,
        intent_router=router,
        state_monitor=object(),
        state_store=FakeStateStore(manager),
    )
    app.config["TESTING"] = True
    return app.test_client(), router, manager, responder


class ServerIntentRoutingTest(unittest.TestCase):
    def test_task_query_routes_once_without_mutation(self):
        client, router, manager, responder = build_client({
            "intent": "query", "target_task_id": "T1", "query_scope": "task",
            "confirm_stage": None, "decision": None, "action": None,
            "needs_clarification": False, "query_topics": ["location", "equipment"],
        })

        response = client.post("/api/query", json={"message": "progress", "task_id": "T1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["type"], "query")
        self.assertEqual(len(router.calls), 1)
        self.assertEqual(manager.set_calls, [])
        self.assertEqual(responder.query_calls[0]["query_topics"], ["location", "equipment"])

    def test_safe_query_fallback_requests_clarification(self):
        client, router, manager, responder = build_client({
            "intent": "query", "target_task_id": None, "query_scope": "task",
            "confirm_stage": None, "decision": None, "action": None,
            "needs_clarification": True, "reason": "low confidence",
        })

        response = client.post("/api/query", json={"message": "unclear", "task_id": "T1"})

        self.assertEqual(response.get_json()["type"], "query")
        self.assertEqual(responder.query_clarification_calls, [("unclear", "low confidence")])
        self.assertEqual(manager.set_calls, [])

    def test_control_request_creates_pending_action(self):
        action = {"action": "rollback", "to_subtask": "S1"}
        client, router, manager, responder = build_client({
            "intent": "confirm", "target_task_id": "T1", "query_scope": None,
            "confirm_stage": "request", "decision": None, "action": action,
            "needs_clarification": False,
        })

        response = client.post("/api/query", json={"message": "rollback", "task_id": "T1"})

        self.assertEqual(response.get_json()["type"], "intervention_pending")
        self.assertEqual(manager.set_calls[0]["action"], action)
        self.assertEqual(len(router.calls), 1)

    def test_confirmation_executes_existing_pending_action(self):
        pending = {"action": {"action": "retry", "subtask_id": "S1"}, "user_message": "retry"}
        client, router, manager, responder = build_client({
            "intent": "confirm", "target_task_id": "T1", "query_scope": None,
            "confirm_stage": "decision", "decision": "confirm", "action": None,
            "needs_clarification": False,
        }, pending=pending)

        response = client.post("/api/query", json={"message": "confirm", "task_id": "T1"})

        self.assertEqual(response.get_json()["type"], "intervention")
        self.assertEqual(manager.execute_calls, [("T1", pending["action"])])
        self.assertEqual(manager.clear_calls, ["T1"])

    def test_cancellation_clears_without_executing_pending_action(self):
        pending = {"action": {"action": "retry", "subtask_id": "S1"}, "user_message": "retry"}
        client, router, manager, responder = build_client({
            "intent": "confirm", "target_task_id": "T1", "query_scope": None,
            "confirm_stage": "decision", "decision": "cancel", "action": None,
            "needs_clarification": False,
        }, pending=pending)

        response = client.post("/api/query", json={"message": "cancel", "task_id": "T1"})

        self.assertEqual(response.get_json()["type"], "intervention_cancelled")
        self.assertEqual(manager.execute_calls, [])
        self.assertEqual(manager.clear_calls, ["T1"])

    def test_query_about_pending_action_does_not_execute_it(self):
        pending = {"action": {"action": "retry", "subtask_id": "S1"}, "user_message": "retry"}
        client, router, manager, responder = build_client({
            "intent": "query", "target_task_id": "T1", "query_scope": "task",
            "confirm_stage": None, "decision": None, "action": None,
            "needs_clarification": False,
        }, pending=pending)

        response = client.post("/api/query", json={"message": "what is the impact", "task_id": "T1"})

        self.assertEqual(response.get_json()["type"], "query")
        self.assertEqual(manager.execute_calls, [])
        self.assertEqual(manager.clear_calls, [])

    def test_new_control_request_does_not_replace_existing_pending_action(self):
        pending = {"action": {"action": "retry", "subtask_id": "S1"}, "user_message": "retry"}
        client, router, manager, responder = build_client({
            "intent": "confirm", "target_task_id": "T1", "query_scope": None,
            "confirm_stage": "request", "decision": None,
            "action": {"action": "rollback", "to_subtask": "S1"},
            "needs_clarification": False,
        }, pending=pending)

        response = client.post("/api/query", json={"message": "rollback", "task_id": "T1"})

        self.assertEqual(response.get_json()["type"], "intervention_pending")
        self.assertEqual(manager.set_calls, [])
        self.assertEqual(response.get_json()["pending_action"], pending["action"])

    def test_incomplete_control_request_does_not_create_pending_action(self):
        client, router, manager, responder = build_client({
            "intent": "confirm", "target_task_id": "T1", "query_scope": None,
            "confirm_stage": "request", "decision": None, "action": None,
            "needs_clarification": True, "reason": "missing value",
        })

        response = client.post("/api/query", json={"message": "change parameter", "task_id": "T1"})

        self.assertEqual(response.get_json()["type"], "irrelevant")
        self.assertEqual(manager.set_calls, [])

    def test_global_query_uses_single_route_call(self):
        client, router, manager, responder = build_client({
            "intent": "query", "target_task_id": "T1", "query_scope": "task",
            "confirm_stage": None, "decision": None, "action": None,
            "needs_clarification": False,
        })

        response = client.post("/api/query", json={"message": "T1 progress", "global_mode": True})

        self.assertEqual(response.get_json()["type"], "query")
        self.assertEqual(len(router.calls), 1)
        self.assertTrue(router.calls[0]["global_mode"])
        summary = router.calls[0]["available_tasks"][0]["task_intent_summary"]
        self.assertEqual(summary["location"]["water_depth_m"], 200.0)

    def test_global_summary_query_uses_available_tasks(self):
        client, router, manager, responder = build_client({
            "intent": "query", "target_task_id": None, "query_scope": "global",
            "confirm_stage": None, "decision": None, "action": None,
            "needs_clarification": False,
        })

        response = client.post("/api/query", json={"message": "all tasks", "global_mode": True})

        self.assertEqual(response.get_json()["type"], "query")
        self.assertEqual(len(router.calls), 1)


if __name__ == "__main__":
    unittest.main()
