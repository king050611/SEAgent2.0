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
    def test_build_query_facts_reads_basic_fields_from_query_fields(self):
        responder = QueryResponder(FakeLLM())

        facts = responder._build_query_facts(
            sample_task_state(),
            [],
            query_fields=["task_id", "task_type", "water_depth", "wellhead_id", "equipment_class", "payload", "support_vessel"],
        )

        self.assertNotIn("task_identity", facts)
        self.assertEqual(facts["basic_info"]["task_id"], "TI1")
        self.assertEqual(facts["basic_info"]["task_type"], "valve_operation")
        self.assertEqual(facts["basic_info"]["water_depth"], 200.0)
        self.assertEqual(facts["basic_info"]["wellhead_id"], "WH-7")
        self.assertEqual(facts["basic_info"]["equipment_class"], "work_class_rov")
        self.assertEqual(facts["basic_info"]["payload"], ["camera", "arm"])
        self.assertEqual(facts["basic_info"]["support_vessel"]["name"], "Vessel-A")
        self.assertNotIn("monitoring_runtime_example", str(facts))

    def test_query_fields_can_be_combined_with_runtime_topics(self):
        responder = QueryResponder(FakeLLM())

        facts = responder._build_query_facts(
            sample_task_state(),
            ["subtask_status", "criteria"],
            query_fields=["water_depth"],
        )

        self.assertEqual(facts["basic_info"]["water_depth"], 200.0)
        self.assertEqual(facts["subtask_status"][0]["status"], "failed")
        self.assertEqual(facts["criteria"]["current_subtask"]["hard_unmet_details"], ["distance_error_max"])

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

    def test_basic_info_query_preserves_llm_query_fields(self):
        responder = QueryResponder(FakeLLM({"intent": "query", "confidence": 0.95, "query_topics": [], "query_fields": ["water_depth"]}))

        result = responder._classify_intent("当前水深是多少", sample_task_state())

        self.assertEqual(result["intent"], "query")
        self.assertEqual(result["query_topics"], [])
        self.assertEqual(result["query_fields"], ["water_depth"])

    def test_unknown_query_fields_are_discarded(self):
        responder = QueryResponder(FakeLLM({"intent": "query", "confidence": 0.95, "query_topics": [], "query_fields": ["water_depth", "current_weather"]}))

        result = responder._classify_intent("当前水深和天气是多少", sample_task_state())

        self.assertEqual(result["query_fields"], ["water_depth"])

    def test_generate_reply_strips_model_thinking_process(self):
        responder = QueryResponder(FakeLLM(text=(
            "Thinking Process:\n"
            "1. Analyze the Request: internal reasoning.\n"
            "2. Determine the Content: more internal reasoning.\n\n"
            "Final Reply:\n"
            "当前没有待确认操作。请先发起明确的重试、回退、修改或人工完成请求。"
        )))

        reply = responder.generate_reply(
            reply_intent="当前没有待确认的流程控制或写入请求。请说明用户需要先发起明确动作。",
            user_message="确认",
            task_state=sample_task_state(),
            operation_result={"error": "no_pending_intervention_to_confirm", "message": "当前没有待确认操作。"},
        )

        self.assertNotIn("Thinking Process", reply)
        self.assertNotIn("Analyze the Request", reply)
        self.assertIn("当前没有待确认操作", reply)

    def test_classify_prompt_includes_available_criteria_semantics(self):
        llm = FakeLLM({"intent": "irrelevant", "confidence": 0.1})
        responder = QueryResponder(llm)

        responder._classify_intent("修改最大误差距离为0.15", sample_task_state())

        prompt = llm.extract_calls[0][0]["content"]
        self.assertIn("当前可用判据及语义", prompt)
        self.assertIn('"key": "distance_error_max"', prompt)
        self.assertIn('"name": "距离误差"', prompt)
        self.assertIn('"kind": "hard"', prompt)
        self.assertIn('"current_value": 0.1', prompt)

    def test_control_and_write_actions_are_separated(self):
        control = QueryResponder(FakeLLM({"intent": "control", "confidence": 0.95, "action": {"action": "rollback", "to_subtask": "S1"}}))
        write = QueryResponder(FakeLLM({"intent": "write", "confidence": 0.95, "action": {"action": "override_field", "subtask_id": "S1", "field": "distance_error_max", "value": 0.05}}))

        self.assertEqual(control._classify_intent("回退到S1", sample_task_state())["intent"], "control")
        self.assertEqual(write._classify_intent("人工确认距离误差为0.05", sample_task_state())["intent"], "write")

    def test_action_type_must_match_explicit_control_word(self):
        responder = QueryResponder(FakeLLM({"intent": "control", "confidence": 0.95, "action": {"action": "force_complete", "subtask_id": "S1"}}))

        result = responder._classify_intent("回退到S1", sample_task_state())

        self.assertEqual(result["intent"], "irrelevant")
        self.assertTrue(result["needs_clarification"])

    def test_standalone_confirmation_is_not_force_complete_without_pending_context(self):
        responder = QueryResponder(FakeLLM({"intent": "control", "confidence": 0.95, "action": {"action": "force_complete", "subtask_id": "S1"}}))

        result = responder._classify_intent("确认", sample_task_state())

        self.assertEqual(result["intent"], "irrelevant")
        self.assertTrue(result["needs_clarification"])

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
