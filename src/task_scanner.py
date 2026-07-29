"""
task_scanner.py – 扫描 task 文件夹下的 JSON 准入文件并创建任务（支持轮询调用）
"""

import json
from pathlib import Path
from typing import List, Set
from .state_store import StateStore
from .task_manager import TaskManager


class TaskScanner:
    def __init__(self, task_folder: str, task_manager: TaskManager, state_store: StateStore, record_file: str = "processed_tasks.json"):
        self.task_folder = Path(task_folder)
        self.task_manager = task_manager
        self.state_store = state_store
        self.record_file = Path(record_file)
        self.processed_ids: Set[str] = self._load_processed()

    def _load_processed(self) -> Set[str]:
        if self.record_file.exists():
            try:
                data = json.loads(self.record_file.read_text(encoding="utf-8"))
                return set(data.get("processed_ids", []))
            except:
                return set()
        return set()

    def _save_processed(self):
        self.record_file.write_text(json.dumps({"processed_ids": list(self.processed_ids)}, indent=2), encoding="utf-8")

    def scan_and_create(self) -> List[dict]:
        """扫描 task 文件夹，为每个未处理的 JSON 文件创建任务，返回结果列表（每个文件一条记录）"""
        if not self.task_folder.exists():
            self.task_folder.mkdir(parents=True, exist_ok=True)
            return []

        results = []
        for file_path in self.task_folder.glob("*.json"):
            # 如果文件已处理，跳过
            if file_path.name in self.processed_ids:
                continue

            try:
                content = json.loads(file_path.read_text(encoding="utf-8"))
                intent_id = content.get("intent_id")
                task_type = content.get("task_type")

                if not intent_id or not task_type:
                    results.append({"file": file_path.name, "status": "skipped", "reason": "missing intent_id or task_type"})
                    continue

                # 检查任务是否已存在（防止重复创建）
                if self.state_store.get_task(intent_id):
                    results.append({"file": file_path.name, "status": "skipped", "reason": "task already exists"})
                    self.processed_ids.add(file_path.name)   # 标记为已处理，避免反复尝试
                    continue

                # 根据任务类型生成描述
                description = self._task_type_to_description(task_type)
                # 创建任务，传入完整 intent 内容作为 initial_data（metadata）
                result = self.task_manager.create_new_task(intent_id, description, content)
                if result.get("ok"):
                    results.append({"file": file_path.name, "status": "created", "task_id": intent_id})
                    self.processed_ids.add(file_path.name)
                else:
                    results.append({"file": file_path.name, "status": "failed", "reason": result.get("error")})
            except Exception as e:
                # 单个文件解析失败不影响其他文件
                results.append({"file": file_path.name, "status": "error", "reason": str(e)})

        self._save_processed()
        return results

    def _task_type_to_description(self, task_type: str) -> str:
        """任务类型到自然语言描述的映射（可扩展）"""
        mapping = {
            "valve_operation": "执行采油树控制面板插头插入任务",
            # 可扩展其他任务类型，如 "connector_mating": "执行水下连接器对接任务"
        }
        return mapping.get(task_type, f"执行 {task_type} 任务")