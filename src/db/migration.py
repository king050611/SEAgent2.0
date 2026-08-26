"""
db/migration.py – One-way migration tool: copy existing JSON file tasks
(data/tasks/*.json) into the new PostgreSQL / SQLite DBStateStore.

Run:
    python -m src.db.migration [--storage_dir data/tasks] [--db_url sqlite:///data/tasks.db]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _find_all_tasks(storage_dir: str) -> List[Path]:
    p = Path(storage_dir)
    p.mkdir(parents=True, exist_ok=True)
    return sorted(p.glob("*.json"))


def _validate_task_state(state: Dict[str, Any]) -> bool:
    required = ("task_id", "description", "subtasks")
    missing = [k for k in required if k not in state]
    if missing:
        logger.error("State missing fields: %s", missing)
        return False
    return True


def run_migration(storage_dir: str, db_url: str = None, dry_run: bool = False) -> Dict[str, Any]:
    from .store import DBStateStore

    tasks = _find_all_tasks(storage_dir)
    logger.info("Found %d candidate task files in %s", len(tasks), storage_dir)

    if dry_run:
        return {"dry_run": True, "total": len(tasks), "migrated": 0, "errors": 0, "skipped": 0}

    store = DBStateStore(storage_dir=storage_dir, db_url=db_url)
    ok = 0
    err = 0
    skip = 0
    t0 = time.time()
    for file_path in tasks:
        try:
            with file_path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
            if not _validate_task_state(state):
                logger.warning("Skip invalid state: %s", file_path.name)
                skip += 1
                continue
            if store.get_task(state["task_id"]) is not None:
                # idempotent re-run: do not overwrite
                skip += 1
                continue
            store.save_task(state["task_id"], state)
            ok += 1
        except Exception as exc:
            err += 1
            logger.exception("Failed to migrate %s: %s", file_path.name, exc)
    dt = time.time() - t0
    summary = {
        "total": len(tasks),
        "migrated": ok,
        "errors": err,
        "skipped": skip,
        "duration_seconds": round(dt, 3),
    }
    logger.info("Migration done: %s", summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Migrate JSON tasks to the DB store.")
    parser.add_argument("--storage_dir", default="data/tasks")
    parser.add_argument("--db_url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    summary = run_migration(args.storage_dir, db_url=args.db_url, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("errors", 0) == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
