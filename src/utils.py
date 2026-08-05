"""
utils.py – 通用工具函数
"""

import yaml
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict


def load_yaml_config(path: Path) -> Dict[str, Any]:
    """加载 YAML 配置文件"""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_logger(name: str, log_file: str = "task_monitor.log", level=logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(ch)
    return logger


def current_timestamp() -> float:
    return datetime.now().timestamp()


def resolve_path_from_base(path: str | Path, base_dir: Path) -> Path:
    """Resolve a config path relative to the project base directory."""
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    return base_dir / resolved