import unittest

from src.server import create_app


class FakeResponder:
    def __init__(self, result=None, decision=None):
        self.result = result or {"type": "query", "intent": "query", "answer": "query answer"}
        self.decision = decision or {"decision": "other", "confidence": 0.9}
        self.process_calls = []
        self.confirm_calls = []

    def process(self, user_message, task_state):
        self.process_calls.append((user_message, task_state))
        return dict(self.result)

    def process_global(self, user_message, all_tasks, task_manager):
        return dict(self.result)

    def classify_confirmation(self, user_message, pending_intervention, task_state):
        self.confirm_calls.append((user_message, pending_intervention, task_state))
        return dict(self.decision)

    def generate_reply(self, **kwargs):
        return "generated reply"

    def generate_confirmation_request(self, user_message, action, task_state, intent=None):
        return f"confirm {intent or 'action'}"

    def generate_intervention_response(self, user_message, intervention_result, task_state, intent=None):
        return f"executed {intent or 'action'}"


class FakeTaskManager:
    def __init__(self, pending=None):
        self.state = {
            "task_id": "T1",
            "description": "task",
            "overall_status": "in_progress",
            "current_subtask": "S1",
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
        self.state["pending_intervention"] = {"action": kwargs["action"], "intent": kwargs.get("raw_intent", {}).get("intent")}
        return {"ok": True, "pending_intervention": self.state["pending_intervention"]}

    def clear_pending_intervention(self, task_id):
        self.clear_calls.append(task_id)
        pending = self.state.pop("pending_intervention", None)
        return {"ok": True, "pending_intervention": pending}

    def execute_intervention(self, task_id, action):
        self.execute_calls.append((task_id, action))
        return {"ok": True, **action}


class FakeStateStore:
    def __init__(self, manager):
        self.manager = manager

    def list_tasks(self):
        return {"T1": dict(self.manager.state)}


def build_client(responder, pending=None):
    manager = FakeTaskManager(pending=pending)
    app = create_app(
        task_manager=manager,
        query_responder=responder,
        state_monitor=object(),
        state_store=FakeStateStore(manager),
    )
    app.config["TESTING"] = True
    return app.test_client(), manager


class ServerIntentFlowTest(unittest.TestCase):
    def test_new_control_request_creates_pending_with_intent(self):
        responder = FakeResponder({"type": "control", "intent": "control", "action": {"action": "retry", "subtask_id": "S1"}, "raw_intent": {"intent": "control"}})
        client, manager = build_client(responder)

        response = client.post("/api/query", json={"message": "重试S1", "task_id": "T1"})

        body = response.get_json()
        self.assertEqual(body["type"], "intervention_pending")
        self.assertEqual(body["intent"], "control")
        self.assertEqual(manager.set_calls[0]["action"]["action"], "retry")

    def test_new_write_request_creates_pending_with_intent(self):
        responder = FakeResponder({"type": "write", "intent": "write", "action": {"action": "override_field", "subtask_id": "S1", "field": "distance_error_max", "value": 0.05}, "raw_intent": {"intent": "write"}})
        client, manager = build_client(responder)

        response = client.post("/api/query", json={"message": "人工确认距离误差0.05", "task_id": "T1"})

        body = response.get_json()
        self.assertEqual(body["type"], "intervention_pending")
        self.assertEqual(body["intent"], "write")
        self.assertEqual(manager.set_calls[0]["action"]["action"], "override_field")

    def test_pending_query_is_answered_without_clearing_pending(self):
        pending = {"action": {"action": "retry", "subtask_id": "S1"}, "intent": "control", "user_message": "重试S1"}
        responder = FakeResponder({"type": "query", "intent": "query", "answer": "水深200米"})
        client, manager = build_client(responder, pending=pending)

        response = client.post("/api/query", json={"message": "当前水深是多少", "task_id": "T1"})

        body = response.get_json()
        self.assertEqual(body["type"], "query")
        self.assertEqual(body["intent"], "query")
        self.assertEqual(manager.clear_calls, [])
        self.assertEqual(manager.execute_calls, [])

    def test_pending_rejects_new_control_without_replacing(self):
        pending = {"action": {"action": "retry", "subtask_id": "S1"}, "intent": "control", "user_message": "重试S1"}
        responder = FakeResponder({"type": "control", "intent": "control", "action": {"action": "rollback", "to_subtask": "S1"}, "raw_intent": {"intent": "control"}})
        client, manager = build_client(responder, pending=pending)

        response = client.post("/api/query", json={"message": "回退到S1", "task_id": "T1"})

        body = response.get_json()
        self.assertEqual(body["type"], "intervention_pending")
        self.assertEqual(body["intent"], "control")
        self.assertEqual(body["pending_action"], pending["action"])
        self.assertEqual(manager.set_calls, [])

    def test_confirm_executes_original_pending_action(self):
        pending = {"action": {"action": "retry", "subtask_id": "S1"}, "intent": "control", "user_message": "重试S1"}
        responder = FakeResponder(decision={"decision": "confirm", "confidence": 0.99})
        client, manager = build_client(responder, pending=pending)

        response = client.post("/api/query", json={"message": "确认", "task_id": "T1"})

        body = response.get_json()
        self.assertEqual(body["type"], "intervention")
        self.assertEqual(body["intent"], "control")
        self.assertEqual(manager.execute_calls, [("T1", pending["action"])])
        self.assertEqual(manager.clear_calls, ["T1"])

    def test_cancel_clears_original_pending_action(self):
        pending = {"action": {"action": "retry", "subtask_id": "S1"}, "intent": "control", "user_message": "重试S1"}
        responder = FakeResponder(decision={"decision": "cancel", "confidence": 0.99})
        client, manager = build_client(responder, pending=pending)

        response = client.post("/api/query", json={"message": "取消", "task_id": "T1"})

        body = response.get_json()
        self.assertEqual(body["type"], "intervention_cancelled")
        self.assertEqual(body["intent"], "control")
        self.assertEqual(manager.execute_calls, [])
        self.assertEqual(manager.clear_calls, ["T1"])


if __name__ == "__main__":
    unittest.main()
