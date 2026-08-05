import unittest

from src.query_responder import QueryResponder


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def generate(self, messages, temperature=0.35, max_tokens=800):
        self.prompts.append(messages[0]["content"])
        return "reply"


class QueryResponderControlTest(unittest.TestCase):
    def test_action_generation_failure_uses_model_failure_reply_intent(self):
        llm = FakeLLM()
        responder = QueryResponder(llm)

        responder.answer_control_clarification(
            "回退到S1",
            {
                "task_id": "T1",
                "overall_status": "in_progress",
                "current_subtask": "S2",
                "subtasks": [],
            },
            "action_compile_generation_failed: rollback 缺少必填字段 to_subtask",
        )

        self.assertIn("未能生成符合执行协议的控制结构", llm.prompts[0])
        self.assertNotIn("请用户补充动作", llm.prompts[0])


if __name__ == "__main__":
    unittest.main()
