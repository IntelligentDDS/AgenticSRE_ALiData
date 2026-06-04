"""
AgenticSRE Expert Feedback Store
Persists expert feedback and activates supervised learning via ContextLearner.learn_supervised().
SOW: "利用专家反馈...构建智能体上下文，实现多智能体运维能力的持续演化"
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_FEEDBACK_DIR = os.path.expanduser("./data/expert_feedback")


class ExpertFeedbackStore:
    """
    Stores expert feedback entries and triggers supervised learning
    through ContextLearner.learn_supervised().

    Usage:
        store = ExpertFeedbackStore()
        store.submit_feedback("rca-001", "OOMKill root cause", context_learner=learner)
    """

    def __init__(self, feedback_dir: Optional[str] = None):
        self._dir = Path(feedback_dir or _DEFAULT_FEEDBACK_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._feedback_file = self._dir / "feedback_entries.json"
        self._entries: List[Dict] = []
        self._load()

    def _load(self):
        if self._feedback_file.exists():
            try:
                self._entries = json.loads(self._feedback_file.read_text(encoding="utf-8"))
            except Exception:
                self._entries = []

    def _save(self):
        self._feedback_file.write_text(
            json.dumps(self._entries, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def submit_feedback(
        self,
        incident_id: str,
        expert_diagnosis: str,
        comment: str = "",
        context_learner=None,
        trace_store=None,
        fault_store=None,
    ) -> Dict:
        """
        Submit expert feedback for a past incident.

        When trace_store is provided, the real agent diagnosis is looked up from the
        pipeline trace (keyed by incident_id == session_id) and passed to
        learn_supervised() — the supervised LLM call now compares real RCA output vs
        the expert answer instead of a placeholder string.

        When fault_store is provided, rules that were surfaced into the original RCA
        prompt (final_result["used_rule_ids"]) are voted on via record_rule_usage()
        — positive if the supervised learner judged the diagnosis correct, negative
        otherwise. This is the last mile of the evolution loop.

        Returns dict with feedback_id, learning_status, rules_generated, rules_voted.
        """
        feedback_id = f"fb-{uuid.uuid4().hex[:8]}"
        entry: Dict[str, Any] = {
            "feedback_id": feedback_id,
            "incident_id": incident_id,
            "expert_diagnosis": expert_diagnosis,
            "comment": comment,
            "timestamp": time.time(),
            "learning_status": "no_learner",
            "rules_generated": 0,
            "rules_voted": 0,
            "correct": None,
        }

        real_diagnosis: Optional[str] = None
        used_rule_ids: List[str] = []
        if trace_store is not None:
            trace = trace_store.get_trace_by_id(incident_id)
            if trace is not None and isinstance(trace.result, dict):
                root_cause = str(trace.result.get("root_cause", "")).strip()
                reasoning = str(trace.result.get("reasoning_chain", "")).strip()
                if root_cause or reasoning:
                    real_diagnosis = f"Root cause: {root_cause}\nReasoning: {reasoning[:1000]}"
                used_rule_ids = list(trace.result.get("used_rule_ids", []) or [])

        # Activate supervised learning
        if context_learner is not None:
            try:
                if real_diagnosis is None:
                    # Fallback: legacy placeholder path so feedback without trace still records
                    agent_diagnosis = f"[Incident {incident_id}] no pipeline trace available"
                    entry["learning_status_note"] = "no_trace_match"
                else:
                    agent_diagnosis = real_diagnosis

                learn_result = context_learner.learn_supervised(
                    agent_diagnosis=agent_diagnosis,
                    ground_truth=expert_diagnosis,
                )
                entry["learning_status"] = learn_result.get("status", "unknown")
                entry["rules_generated"] = learn_result.get("rules_added", 0)
                entry["correct"] = learn_result.get("correct")

                if (
                    fault_store is not None
                    and used_rule_ids
                    and entry["learning_status"] == "success"
                    and entry["correct"] is not None
                ):
                    bump = fault_store.record_rule_usage(
                        used_rule_ids, positive=bool(entry["correct"])
                    )
                    entry["rules_voted"] = bump.get("updated", 0)

                logger.info(
                    "[ExpertFeedback] Supervised: status=%s rules=%d voted=%d correct=%s",
                    entry["learning_status"],
                    entry["rules_generated"],
                    entry["rules_voted"],
                    entry["correct"],
                )
            except Exception as e:
                entry["learning_status"] = f"error: {e}"
                logger.warning("[ExpertFeedback] Supervised learning failed: %s", e)

        self._entries.append(entry)
        self._save()

        return {
            "feedback_id": feedback_id,
            "incident_id": incident_id,
            "learning_status": entry["learning_status"],
            "rules_generated": entry["rules_generated"],
            "rules_voted": entry["rules_voted"],
            "correct": entry["correct"],
        }

    def get_recent_feedback(self, n: int = 10) -> List[Dict]:
        """Return the N most recent feedback entries."""
        return list(reversed(self._entries[-n:]))

    def get_feedback_stats(self) -> Dict:
        """Aggregate feedback statistics."""
        total = len(self._entries)
        with_rules = sum(1 for e in self._entries if e.get("rules_generated", 0) > 0)
        total_rules = sum(e.get("rules_generated", 0) for e in self._entries)
        return {
            "total": total,
            "with_rules": with_rules,
            "total_rules_generated": total_rules,
            "success_rate": with_rules / max(total, 1),
        }
