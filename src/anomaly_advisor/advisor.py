"""Top-level anomaly advisor orchestration."""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from .context import build_anomaly_context, get_current_subtask, get_latest_anomaly_context
from .llm_generator import AnomalyAdviceGenerator
from .rules import AnomalyRuleMatcher


class AnomalyAdvisor:
    """Side-channel anomaly advice coordinator.

    The advisor reads backend-provided anomaly_state and task context to produce
    diagnostic advice. It does not execute retry, rollback, manual approval, or
    fail-task actions by itself.
    """

    def __init__(self, llm_client: Optional[Any] = None):
        self.rule_matcher = AnomalyRuleMatcher()
        self.generator = AnomalyAdviceGenerator(llm_client=llm_client)

    def record_anomaly_context(
        self,
        task_state: Dict[str, Any],
        subtask: Dict[str, Any],
        failed_criteria: Optional[list[str]] = None,
        anomaly_key: Optional[str] = None,
        system_action: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record latest anomaly context on the provided task_state dict.

        This method intentionally does not call the LLM, generate user-facing
        advice, or mutate workflow status.
        """
        context = build_anomaly_context(
            task_state=task_state,
            subtask=subtask,
            failed_criteria=failed_criteria,
            anomaly_key=anomaly_key,
            system_action=system_action,
        )
        task_state["latest_anomaly_context"] = copy.deepcopy(context)
        return context

    def generate_advice_for_latest_anomaly(
        self,
        task_state: Dict[str, Any],
        user_question: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate advice from task_state.latest_anomaly_context.

        If no recorded context exists, this method builds one from the current
        subtask and task-level anomaly_state as a convenience for integration.
        """
        context = get_latest_anomaly_context(task_state)
        if context is None:
            subtask = get_current_subtask(task_state)
            if not subtask:
                return self._no_context_result()
            criteria = subtask.get("completion_criteria") or {}
            context = build_anomaly_context(
                task_state=task_state,
                subtask=subtask,
                failed_criteria=criteria.get("hard_unmet_details", []),
                anomaly_key=None,
                system_action=None,
            )
        return self.generate_advice(context, user_question=user_question)

    def generate_advice(
        self,
        context: Dict[str, Any],
        user_question: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Match anomaly_state to configured advice and render a response."""
        classification = self.rule_matcher.classify(context)
        advice = self.generator.generate(
            context=context,
            classification=classification,
            user_question=user_question,
        )
        advice["classification"] = classification
        return advice

    def _no_context_result(self) -> Dict[str, Any]:
        return {
            "advice_generated": False,
            "adoption_flag": False,
            "exception_category": "暂无可用异常建议",
            "summary": "当前任务状态中没有可用于生成异常建议的上下文。",
            "advice_text": "当前没有可用于生成异常建议的任务上下文。请确认任务状态中包含 current_subtask 和 anomaly_state。",
            "missing_evidence": ["current_subtask", "anomaly_state"],
            "matched_anomalies": [],
        }
