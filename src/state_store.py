"""
state_store.py – 任务状态持久化（按任务ID分文件存储）
增加原子更新方法，避免并发冲突
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from threading import Lock


class StateStore:
    def __init__(self, storage_dir: str):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _get_path(self, task_id: str) -> Path:
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', task_id)
        return self.storage_dir / f"{safe_id}.json"

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        path = self._get_path(task_id)
        if not path.exists():
            return None
        with self._lock:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None

    def _atomic_write_json(self, path: Path, data: Dict[str, Any]):
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)

    def save_task(self, task_id: str, task_state: Dict[str, Any]):
        """直接保存（不推荐直接使用，建议使用 update_task_atomic）"""
        path = self._get_path(task_id)
        with self._lock:
            self._atomic_write_json(path, task_state)

    def update_task_atomic(self, task_id: str, update_func: Callable[[Dict[str, Any]], None]) -> Optional[Dict[str, Any]]:
        """
        原子更新：在锁内获取当前状态，调用 update_func 修改，然后保存。
        返回更新后的状态，若任务不存在则返回 None。
        """
        path = self._get_path(task_id)
        with self._lock:
            if not path.exists():
                return None
            try:
                task_state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
            # 调用修改函数
            update_func(task_state)
            # 写回文件：先写临时文件再原子替换，避免进程异常导致 JSON 半写入
            self._atomic_write_json(path, task_state)
            return task_state

    def delete_task(self, task_id: str):
        path = self._get_path(task_id)
        if path.exists():
            with self._lock:
                path.unlink()

    def list_tasks(self) -> Dict[str, Any]:
        tasks = {}
        with self._lock:
            for file_path in self.storage_dir.glob("*.json"):
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    task_id = data.get("task_id")
                    if task_id:
                        tasks[task_id] = data
                except Exception:
                    continue
        return tasks