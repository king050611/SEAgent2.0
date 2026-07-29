"""
task_decomposer.py – 根据任务模板生成子任务实例
"""

from typing import Dict, Any, List
from copy import deepcopy


class TaskDecomposer:
    def __init__(self, templates: Dict[str, Any]):
        self.templates = templates
        self.subtask_templates = templates.get("subtasks", [])

    def decompose(self, task_description: str, initial_params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """根据任务描述（目前只支持采油树插入）生成子任务列表"""
        subtasks = []
        for template in self.subtask_templates:
            subtask = deepcopy(template)
            # 统一字段名：将 id 改为 subtask_id
            subtask["subtask_id"] = subtask.pop("id")
            # 初始化运行时状态
            subtask["status"] = "pending"
            subtask["retry_count"] = 0
            # 完成判据的具体内容由 CriteriaEvaluator 根据 criteria_ref 从 criteria.yaml 加载
            subtask["completion_criteria"] = {}   # 初始为空，评估时会填充
            subtask["evidence_summary"] = ""
            subtask["latest_state"] = {}
            subtasks.append(subtask)
        return subtasks

    def get_subtask_by_id(self, subtask_id: str) -> Dict[str, Any]:
        for t in self.subtask_templates:
            if t["id"] == subtask_id:
                # 返回模板的深拷贝，注意这里保持原始模板结构（id字段）
                return deepcopy(t)
        return None