"""
Utility helpers.
"""

import logging
import random
import time
import uuid
from typing import Any, Dict, Optional


def get_logger(name: str = "curlx") -> logging.Logger:
    """Get a structured logger."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def generate_request_id() -> str:
    """Generate a unique request ID."""
    return str(uuid.uuid4())


def jitter(base: float, jitter_factor: float = 0.1) -> float:
    """Add random jitter to a base value."""
    return base + random.uniform(-base * jitter_factor, base * jitter_factor)


def merge_dicts(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge two dicts; override wins on key collision."""
    result = dict(base)
    if override:
        result.update(override)
    return result


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp a value between min and max."""
    return max(min_val, min(value, max_val))
