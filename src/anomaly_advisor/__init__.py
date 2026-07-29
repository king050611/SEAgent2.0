"""Anomaly advice package.

This package is intentionally side-effect light: it reads task/subtask state and
generates diagnostic advice, but it does not retry, rollback, or change task
status by itself.
"""

from .advisor import AnomalyAdvisor

__all__ = ["AnomalyAdvisor"]
