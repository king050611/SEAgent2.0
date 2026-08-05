import unittest

from src.task_intent_retriever import retrieve_task_intent, resolve_task_intent


class TaskIntentRetrieverTest(unittest.TestCase):
    def test_resolves_scanner_metadata_shape(self):
        state = {
            "metadata": {
                "intent_id": "TI1",
                "task_type": "valve_operation",
                "location": {"water_depth_m": 200.0},
            }
        }

        self.assertEqual(resolve_task_intent(state)["intent_id"], "TI1")

    def test_resolves_from_intent_metadata_shape(self):
        state = {
            "metadata": {
                "intent": {
                    "intent_id": "TI2",
                    "task_type": "valve_operation",
                    "equipment": {"robot_type": "work_class_rov"},
                }
            }
        }

        self.assertEqual(resolve_task_intent(state)["intent_id"], "TI2")

    def test_retrieves_only_requested_static_topics(self):
        state = {
            "metadata": {
                "intent_id": "TI1",
                "task_type": "valve_operation",
                "priority": "high",
                "time": {"start": "2026-02-11T09:00:00Z"},
                "location": {"oilfield": "A", "water_depth_m": 200.0},
                "task": {
                    "details": {
                        "wellhead_id": "W1",
                        "target_latitude": 12.3,
                        "target_longitude": 45.6,
                    }
                },
                "equipment": {"robot_type": "work_class_rov"},
                "conditions": {"visibility": "low"},
                "monitoring_runtime_example": {"current_stage": "S2"},
            },
            "current_subtask": "S5",
        }

        facts = retrieve_task_intent(state, ["location", "equipment", "runtime"])

        self.assertEqual(facts["任务位置"]["water_depth_m"], 200.0)
        self.assertEqual(facts["作业设备"]["robot_type"], "work_class_rov")
        self.assertNotIn("monitoring_runtime_example", str(facts))
        self.assertNotIn("runtime", facts)


if __name__ == "__main__":
    unittest.main()
