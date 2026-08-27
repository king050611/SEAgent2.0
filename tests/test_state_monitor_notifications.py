import unittest

from src.state_monitor import StateMonitor


class DirtyResponder:
    def generate_reply(self, **kwargs):
        return (
            "1. Analyze the Request:\n"
            "Internal reasoning that should never be shown.\n\n"
            "检测到软硬判据均已达标。\n\n"
            "请审核。"
        )


class StateMonitorNotificationTest(unittest.TestCase):
    def test_approval_notification_uses_deterministic_template_not_llm_analysis(self):
        monitor = StateMonitor(
            state_store=None,
            criteria_evaluator=None,
            state_mapping={},
            query_responder=DirtyResponder(),
        )
        task_state = {
            "subtasks": [
                {
                    "subtask_id": "S1",
                    "name": "移动至采油树控制面板附近",
                }
            ]
        }
        criteria_result = {
            "hard_details": {
                "distance_error_max": {"expected": 0.1, "actual": 0.05, "met": True},
                "angle_error_max": {"expected": 10.0, "actual": 5.0, "met": True},
            },
            "soft_details": {
                "panel_visible_flag": {"required": 1, "actual": 1, "met": True},
            },
        }

        notification = monitor._generate_approval_notification("T1", "S1", criteria_result, task_state)

        self.assertNotIn("Analyze the Request", notification)
        self.assertNotIn("Internal reasoning", notification)
        self.assertIn("distance_error_max", notification)
        self.assertIn("实际 0.05", notification)
        self.assertEqual(notification.count("\n\n"), 2)


if __name__ == "__main__":
    unittest.main()
