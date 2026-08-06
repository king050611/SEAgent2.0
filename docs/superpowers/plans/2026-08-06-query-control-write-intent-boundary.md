# Query Control Write Intent Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the current conversation protocol into read-only `query`, flow-changing `control`, data-writing `write`, and safe `irrelevant` paths.

**Architecture:** Keep `QueryResponder` as the conversation coordinator. Use prompt changes for LLM classification, then enforce intent/action safety in code before `server.py` creates pending actions or executes confirmations.

**Tech Stack:** Python 3.12, Flask, unittest, existing local `LLMClient` interface, existing `TaskManager` intervention execution.

## Global Constraints

- Modify only `src/prompts.py`, `src/query_responder.py`, `src/server.py`, and `README.md`.
- Add only `tests/test_query_responder_intents.py` and `tests/test_server_intent_flow.py`.
- Do not change task decomposition, S1-S8 ordering, criteria evaluation, anomaly advice, status update APIs, approval APIs, persistence layout, frontend structure, model loading, config directory structure, or third-party dependencies.
- Keep external response type compatibility by continuing to use `type: "intervention_pending"` for pending control/write actions, with an added `intent` field.
- `override_field` can write only fields declared in the target subtask's `criteria_ref` hard/soft criteria.
- `change_parameter` can write only `timeout_seconds` and `max_retries`.
- `metadata["monitoring_runtime_example"]` must not be included in query facts.

---

### Task 1: QueryResponder Intent Protocol Tests

**Files:**
- Create: `tests/test_query_responder_intents.py`
- Modify: `src/query_responder.py`
- Modify: `src/prompts.py`

**Interfaces:**
- Consumes: `QueryResponder._classify_intent(user_message: str, task_state: dict) -> dict`
- Produces: `QueryResponder.VALID_INTENTS`, `CONTROL_ACTIONS`, `WRITE_ACTIONS`, `QUERY_TOPICS`, `WRITABLE_PARAMETERS`
- Produces: `QueryResponder._build_query_facts(task_state: dict, query_topics: list[str]) -> dict`
- Produces: `QueryResponder._normalize_query_topics(raw_topics: Any) -> list[str]`
- Produces: `QueryResponder._normalize_intent_info(intent_info: dict, task_state: dict, user_message: str) -> dict`

- [ ] **Step 1: Write tests for deterministic query facts and protocol normalization**

Create `tests/test_query_responder_intents.py` with fake LLM responses:

```python
import unittest

from src.query_responder import QueryResponder


class FakeLLM:
    def __init__(self, json_result=None, text="ok"):
        self.json_result = json_result or {}
        self.text = text
        self.extract_calls = []
        self.generate_calls = []

    def extract_json(self, messages, max_tokens=300):
        self.extract_calls.append(messages)
        return dict(self.json_result)

    def generate(self, messages, temperature=0.35, max_tokens=800):
        self.generate_calls.append(messages)
        return self.text


def sample_task_state():
    return {
        "task_id": "TI1",
        "description": "执行采油树控制面板插头插入任务",
        "overall_status": "in_progress",
        "current_subtask": "S1",
        "metadata": {
            "intent_id": "TI1",
            "task_type": "valve_operation",
            "priority": 2,
            "location": {"oilfield": "alpha", "water_depth_m": 200.0},
            "task": {
                "details": {
                    "wellhead_id": "WH-7",
                    "target": {"latitude": 20.0, "longitude": 30.0},
                    "christmas_tree_type": "vertical",
                    "hole_positions": ["port_1", "port_2"],
                }
            },
            "equipment": {
                "robot_type": "work_class_rov",
                "payload": ["camera", "arm"],
                "support_vessel": {"name": "Vessel-A", "latitude": 19.9, "longitude": 30.1},
            },
            "conditions": {"visibility": "good"},
            "time": {"start": "2026-05-29T21:59:21+08:00", "end": None},
            "monitoring_runtime_example": {"current_stage": "S8"},
        },
        "subtasks": [
            {
                "subtask_id": "S1",
                "name": "移动至采油树控制面板附近",
                "status": "failed",
                "retry_count": 1,
                "criteria_ref": "S1_criteria",
                "completion_criteria": {
                    "hard_met": False,
                    "soft_met": True,
                    "hard_unmet_details": ["distance_error_max"],
                    "soft_unmet_details": [],
                    "hard_details": {"distance_error_max": {"expected": 0.1, "actual": 0.15, "met": False}},
                    "soft_details": {},
                },
            }
        ],
        "anomaly_state": {"execution": "abnormal"},
        "latest_anomaly_advice": {"advice_generated": True},
        "pending_intervention": {"action": {"action": "retry", "subtask_id": "S1"}},
    }


class QueryResponderIntentProtocolTest(unittest.TestCase):
    def test_build_query_facts_injects_metadata_and_excludes_runtime_example(self):
        responder = QueryResponder(FakeLLM())

        facts = responder._build_query_facts(
            sample_task_state(),
            ["task_identity", "location", "task_details", "equipment", "conditions", "time"],
        )

        self.assertEqual(facts["task_identity"]["intent_id"], "TI1")
        self.assertEqual(facts["task_identity"]["task_type"], "valve_operation")
        self.assertEqual(facts["location"]["water_depth_m"], 200.0)
        self.assertEqual(facts["task_details"]["target"]["latitude"], 20.0)
        self.assertEqual(facts["equipment"]["robot_type"], "work_class_rov")
        self.assertEqual(facts["equipment"]["support_vessel"]["name"], "Vessel-A")
        self.assertEqual(facts["conditions"]["visibility"], "good")
        self.assertEqual(facts["time"]["start"], "2026-05-29T21:59:21+08:00")
        self.assertNotIn("monitoring_runtime_example", str(facts))

    def test_build_query_facts_injects_runtime_status_criteria_anomaly_and_pending(self):
        responder = QueryResponder(FakeLLM())

        facts = responder._build_query_facts(
            sample_task_state(),
            ["task_status", "subtask_status", "criteria", "anomaly", "pending_action"],
        )

        self.assertEqual(facts["task_status"]["overall_status"], "in_progress")
        self.assertEqual(facts["task_status"]["current_subtask"], "S1")
        self.assertEqual(facts["subtask_status"][0]["status"], "failed")
        self.assertEqual(facts["criteria"]["current_subtask"]["hard_unmet_details"], ["distance_error_max"])
        self.assertEqual(facts["anomaly"]["anomaly_state"], {"execution": "abnormal"})
        self.assertEqual(facts["pending_action"]["action"]["action"], "retry")

    def test_question_with_action_word_is_query(self):
        responder = QueryResponder(FakeLLM({"intent": "control", "confidence": 0.95, "action": {"action": "retry", "subtask_id": "S2"}}))

        result = responder._classify_intent("S2 能重试吗？", sample_task_state())

        self.assertEqual(result["intent"], "query")
        self.assertEqual(result["query_topics"], ["pending_action"])
        self.assertIsNone(result["action"])

    def test_control_and_write_actions_are_separated(self):
        control = QueryResponder(FakeLLM({"intent": "control", "confidence": 0.95, "action": {"action": "rollback", "to_subtask": "S1"}}))
        write = QueryResponder(FakeLLM({"intent": "write", "confidence": 0.95, "action": {"action": "override_field", "subtask_id": "S1", "field": "distance_error_max", "value": 0.05}}))

        self.assertEqual(control._classify_intent("回退到S1", sample_task_state())["intent"], "control")
        self.assertEqual(write._classify_intent("人工确认距离误差为0.05", sample_task_state())["intent"], "write")

    def test_invalid_action_for_intent_requires_clarification(self):
        responder = QueryResponder(FakeLLM({"intent": "control", "confidence": 0.95, "action": {"action": "override_field", "subtask_id": "S1", "field": "distance_error_max", "value": 0.05}}))

        result = responder._classify_intent("覆盖距离误差", sample_task_state())

        self.assertEqual(result["intent"], "irrelevant")
        self.assertTrue(result["needs_clarification"])

    def test_write_missing_value_requires_clarification(self):
        responder = QueryResponder(FakeLLM({"intent": "write", "confidence": 0.95, "action": {"action": "override_field", "subtask_id": "S1", "field": "distance_error_max"}}))

        result = responder._classify_intent("把距离误差改一下", sample_task_state())

        self.assertEqual(result["intent"], "irrelevant")
        self.assertTrue(result["needs_clarification"])

    def test_override_field_must_be_allowed_by_subtask_criteria(self):
        responder = QueryResponder(FakeLLM({"intent": "write", "confidence": 0.95, "action": {"action": "override_field", "subtask_id": "S1", "field": "visual_check_flag", "value": 1}}))

        result = responder._classify_intent("将visual_check_flag覆盖为1", sample_task_state())

        self.assertEqual(result["intent"], "irrelevant")
        self.assertTrue(result["needs_clarification"])

    def test_change_parameter_rejects_unsupported_parameter(self):
        responder = QueryResponder(FakeLLM({"intent": "write", "confidence": 0.95, "action": {"action": "change_parameter", "subtask_id": "S1", "parameter": "hole_id", "value": "port_3"}}))

        result = responder._classify_intent("把孔位改成port_3", sample_task_state())

        self.assertEqual(result["intent"], "irrelevant")
        self.assertTrue(result["needs_clarification"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/root/miniconda3/envs/seagent/bin/python -m unittest tests.test_query_responder_intents -v
```

Expected: fail because `_build_query_facts`, separated action sets, and normalization helpers do not exist yet.

- [ ] **Step 3: Update prompt constants**

In `src/prompts.py`, replace the classification intent language with `query/control/write/irrelevant`, delete `adjust_criterion_tolerance`, define query topics, split control and write examples, and add question-form counterexamples.

- [ ] **Step 4: Implement QueryResponder constants and normalization helpers**

In `src/query_responder.py`, set:

```python
VALID_INTENTS = {"query", "control", "write", "irrelevant"}
CONTROL_ACTIONS = {"rollback", "retry", "force_complete"}
WRITE_ACTIONS = {"change_parameter", "override_field"}
VALID_ACTIONS = CONTROL_ACTIONS | WRITE_ACTIONS
QUERY_TOPICS = {
    "task_identity", "time", "location", "task_details", "equipment", "conditions",
    "task_status", "subtask_status", "criteria", "anomaly", "pending_action",
}
WRITABLE_PARAMETERS = {"timeout_seconds", "max_retries"}
MIN_MUTATION_CONFIDENCE = 0.75
```

Add helpers:

```python
def _normalize_query_topics(self, raw_topics):
    ...

def _looks_like_query(self, user_message):
    ...

def _normalize_intent_info(self, intent_info, task_state, user_message):
    ...

def _allowed_override_fields(self, task_state, subtask_id):
    ...

def _validate_control_action(self, action, task_state):
    ...

def _validate_write_action(self, action, task_state):
    ...

def _build_query_facts(self, task_state, query_topics):
    ...
```

The implementation must convert question-form action phrases to `query`, reject mismatched intent/action pairs as clarification, and never include `monitoring_runtime_example` in query facts.

- [ ] **Step 5: Update `_classify_intent` and `process`**

Change `_classify_intent` to call `_normalize_intent_info` before returning. Change `process` so `query` includes:

```python
operation_result={
    **self._build_query_operation_result(task_state, user_message=user_message),
    "query_topics": intent_info.get("query_topics", []),
    "query_facts": self._build_query_facts(task_state, intent_info.get("query_topics", [])),
}
```

Change mutation returns to:

```python
{"type": "control", "intent": "control", "action": action, "raw_intent": intent_info}
{"type": "write", "intent": "write", "action": action, "raw_intent": intent_info}
```

Clarification failures should return `type: "irrelevant"`, `intent` from the failed classification, and a reply that says the request was understood but cannot be safely staged.

- [ ] **Step 6: Run QueryResponder tests**

Run:

```bash
/root/miniconda3/envs/seagent/bin/python -m unittest tests.test_query_responder_intents -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/prompts.py src/query_responder.py tests/test_query_responder_intents.py
git commit -m "Split responder intent protocol"
```

### Task 2: Server Pending Flow Tests and Orchestration

**Files:**
- Create: `tests/test_server_intent_flow.py`
- Modify: `src/server.py`
- Modify: `src/query_responder.py`

**Interfaces:**
- Consumes: `QueryResponder.process(...)` returning `type` values `query`, `control`, `write`, or `irrelevant`
- Consumes: `QueryResponder.classify_confirmation(...) -> {"decision": "confirm|cancel|other"}`
- Produces: `/api/query` JSON responses with `intent`

- [ ] **Step 1: Write server flow tests**

Create `tests/test_server_intent_flow.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/root/miniconda3/envs/seagent/bin/python -m unittest tests.test_server_intent_flow -v
```

Expected: fail because `server.py` does not branch on `control` and `write`, and pending currently blocks queries.

- [ ] **Step 3: Update `server.py` no-pending branch**

For `result["type"] == "query"`, return `{"type": "query", "intent": "query", ...}`.

For `result["type"] in {"control", "write"}`, call `set_pending_intervention` and return:

```python
{
    "type": "intervention_pending",
    "intent": result["type"],
    "answer": answer,
    "pending_action": action,
    "result": pending_result,
    "refresh_required": False,
}
```

- [ ] **Step 4: Update pending branch**

When `decision == "other"`, call `query_responder.process(user_message, task_state)`.

If that result is query, return its answer and keep pending. If it is control/write, return the existing pending action and do not call `set_pending_intervention`. Confirm and cancel continue to act on the original pending action.

- [ ] **Step 5: Update confirmation response helpers**

Allow optional `intent` parameters in `generate_confirmation_request` and `generate_intervention_response`. Use control-specific language for control actions and write-specific language for write actions.

- [ ] **Step 6: Run server flow tests**

Run:

```bash
/root/miniconda3/envs/seagent/bin/python -m unittest tests.test_server_intent_flow -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/server.py src/query_responder.py tests/test_server_intent_flow.py
git commit -m "Allow query during pending interventions"
```

### Task 3: README and Full Verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: implemented `/api/query` behavior
- Produces: documentation describing query/control/write/irrelevant boundaries and examples

- [ ] **Step 1: Update README intent section**

Add a section explaining:

```markdown
## 三类任务对话意图

- QUERY：只读取任务事实、状态、判据和异常，不修改状态。
- CONTROL：改变流程位置或推进方式，包括 rollback、retry、force_complete。
- WRITE：写入参数或人工覆盖状态事实，包括 change_parameter、override_field。
- IRRELEVANT：与任务无关或无法可靠识别的安全出口。

查询水深、坐标、机器人属于 QUERY。修改水深、坐标、机器人元数据不在本轮 WRITE 范围内。
```

Delete any README examples that describe `adjust_criterion_tolerance`.

- [ ] **Step 2: Run focused tests**

Run:

```bash
/root/miniconda3/envs/seagent/bin/python -m unittest tests.test_query_responder_intents tests.test_server_intent_flow -v
```

Expected: all tests pass.

- [ ] **Step 3: Run full checks**

Run:

```bash
/root/miniconda3/envs/seagent/bin/python -m compileall -q src tests
/root/miniconda3/envs/seagent/bin/python -m unittest discover -s tests -v
```

Expected: compileall exits 0 and all unittest tests pass.

- [ ] **Step 4: Commit Task 3**

```bash
git add README.md
git commit -m "Document conversation intent boundaries"
```
