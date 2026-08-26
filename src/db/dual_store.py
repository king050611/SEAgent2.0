"""
db/dual_store.py – 双写兼容 StateStore（JSON ↔ DB）。

灰度发布策略的一部分：所有写入操作同时写入 JSON（primary）和 DB（secondary）。
读取操作优先从 DB 获取；若 DB 查不到则回退到 JSON，保证上线初期不丢失历史数据。
当 JSON 有但 DB 无时，读操作自动把 JSON 记录同步到 DB（惰性迁移）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from .store import DBStateStore

logger = logging.getLogger(__name__)


class DualWriteStore:
    """StateStore-compatible adapter that writes to both backends."""

    def __init__(self, primary, secondary: DBStateStore, *, migrate_on_read: bool = True):
        self.primary = primary  # JSON StateStore
        self.secondary = secondary  # DBStateStore
        self.migrate_on_read = migrate_on_read
        logger.info("DualWriteStore active: primary=%r secondary=%r", type(primary).__name__, type(secondary).__name__)

    # ---------- helpers ----------
    def _sync_to_secondary(self, task_id: str, state: Dict[str, Any]) -> None:
        try:
            self.secondary.save_task(task_id, state)
        except Exception as exc:
            logger.warning("Dual-write secondary save failed for %s: %s", task_id, exc)

    def _lazy_migrate(self, task_id: str, state: Dict[str, Any]) -> None:
        """首次读取时，把 JSON 存量同步进 DB。"""
        if not self.migrate_on_read:
            return
        try:
            existing = self.secondary.get_task(task_id)
            if existing is None:
                self.secondary.save_task(task_id, state)
                logger.info("Lazy migrated task %s from JSON to DB", task_id)
        except Exception as exc:
            logger.warning("Lazy migrate failed for %s: %s", task_id, exc)

    # ---------- public API (mirrors StateStore / DBStateStore) ----------
    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        # 优先读 DB
        try:
            db_state = self.secondary.get_task(task_id)
            if db_state is not None:
                return db_state
        except Exception as exc:
            logger.warning("Dual-read secondary failed: %s", exc)
        # 回退 JSON → 同步到 DB（惰性迁移）
        json_state = self.primary.get_task(task_id)
        if json_state is not None:
            self._lazy_migrate(task_id, json_state)
        return json_state

    def save_task(self, task_id: str, task_state: Dict[str, Any]) -> None:
        # 先写 primary(JSON) 再写 secondary(DB)；失败不影响 primary 成功
        self.primary.save_task(task_id, task_state)
        self._sync_to_secondary(task_id, task_state)

    def update_task_atomic(self, task_id: str, update_func: Callable[[Dict[str, Any]], None]) -> Optional[Dict[str, Any]]:
        # 原子更新走 primary 保证不丢失；成功后同步
        result = self.primary.update_task_atomic(task_id, update_func)
        if result is not None:
            self._sync_to_secondary(task_id, result)
        return result

    def delete_task(self, task_id: str) -> None:
        self.primary.delete_task(task_id)
        try:
            self.secondary.delete_task(task_id)
        except Exception as exc:
            logger.warning("Dual-write secondary delete failed for %s: %s", task_id, exc)

    def list_tasks(self) -> Dict[str, Any]:
        # 合并两侧结果；以 primary 为准，DB 中存在但 JSON 不存在的极少见情况则补入
        json_tasks = self.primary.list_tasks()
        try:
            db_tasks = self.secondary.list_tasks()
        except Exception as exc:
            logger.warning("Dual-read list secondary failed: %s", exc)
            return json_tasks
        merged: Dict[str, Any] = {}
        for tid, s in json_tasks.items():
            merged[tid] = s
        for tid, s in db_tasks.items():
            if tid not in merged:
                merged[tid] = s
        return merged

    # ---------- audit / cache stats（DB 专属 API，透传）----------
    def write_audit(self, **kwargs: Any) -> None:
        try:
            self.secondary.write_audit(**kwargs)
        except Exception as exc:
            logger.debug("write_audit unavailable: %s", exc)

    def bump_cache_stat(self, layer: str, *, hit: bool) -> None:
        try:
            self.secondary.bump_cache_stat(layer, hit=hit)
        except Exception:
            pass
