"""
criteria_evaluator.py – 评估子任务完成判据（硬+软+人工需求）
"""

from typing import Dict, Any, List
from copy import deepcopy


class CriteriaEvaluator:
    def __init__(self, criteria_config: Dict[str, Any], state_mapping: Dict):
        self.criteria = criteria_config
        self.state_mapping = state_mapping

    def get_criteria(self, ref: str) -> Dict[str, Any]:
        return self.criteria.get(ref, {})

    def evaluate(self, criteria_def: Dict[str, Any], current_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        返回格式：
        {
            "hard_met": bool,
            "hard_details": {...},   # 每个判据的满足情况
            "hard_unmet_details": [list of unmet keys],
            "soft_met": bool,
            "soft_details": {...},
            "soft_unmet_details": [...],
            "require_approval": bool,
            "all_met": bool   # 硬满足且（软满足或没有软判据）
        }
        """
        hard_criteria = criteria_def.get("hard", {})
        soft_criteria = criteria_def.get("soft", {})
        require_approval = criteria_def.get("require_approval", False)

        hard_met = True
        hard_details = {}
        hard_unmet = []
        for key, threshold in hard_criteria.items():
            actual = current_state.get(key)
            if actual is None:
                hard_met = False
                hard_details[key] = {"expected": threshold, "actual": None, "met": False}
                hard_unmet.append(key)
                continue
            if isinstance(threshold, (int, float)):
                if isinstance(actual, (int, float)):
                    met = actual <= threshold if "max" in key or "delta" in key else actual >= threshold
                elif isinstance(actual, bool):
                    met = (actual == threshold) if isinstance(threshold, bool) else (actual == bool(threshold))
                else:
                    met = False
            else:
                met = (actual == threshold)
            hard_details[key] = {"expected": threshold, "actual": actual, "met": met}
            if not met:
                hard_met = False
                hard_unmet.append(key)

        soft_met = True
        soft_details = {}
        soft_unmet = []
        for key, required in soft_criteria.items():
            actual = current_state.get(key)
            if actual is None:
                soft_met = False
                soft_details[key] = {"required": required, "actual": None, "met": False}
                soft_unmet.append(key)
                continue
            if isinstance(required, (int, float)):
                met = actual >= required if "min" in key else actual <= required
            elif isinstance(required, bool):
                met = (actual == required)
            else:
                met = (actual == required)
            soft_details[key] = {"required": required, "actual": actual, "met": met}
            if not met:
                soft_met = False
                soft_unmet.append(key)

        # 如果没有软判据，视为 soft_met = True
        if not soft_criteria:
            soft_met = True

        all_met = hard_met and soft_met
        return {
            "hard_met": hard_met,
            "hard_details": hard_details,
            "hard_unmet_details": hard_unmet,
            "soft_met": soft_met,
            "soft_details": soft_details,
            "soft_unmet_details": soft_unmet,
            "require_approval": require_approval,
            "all_met": all_met
        }