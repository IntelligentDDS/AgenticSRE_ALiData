"""
Human-in-the-loop review queue for RCA quality gates and remediation approvals.

The queue is intentionally file-backed so it works in the cluster deployment
without adding another service dependency.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


_DEFAULT_REVIEW_DIR = "./data/hitl"


class HumanReviewStore:
    """Persistent HITL review queue."""

    def __init__(self, review_dir: Optional[str] = None):
        self._dir = Path(review_dir or _DEFAULT_REVIEW_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._review_file = self._dir / "reviews.json"
        self._reviews: List[Dict[str, Any]] = []
        self._load()

    def _load(self):
        if self._review_file.exists():
            try:
                self._reviews = json.loads(self._review_file.read_text(encoding="utf-8"))
            except Exception:
                self._reviews = []

    def _save(self):
        self._review_file.write_text(
            json.dumps(self._reviews, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def create_review(
        self,
        incident_id: str,
        reason: str,
        rca_result: Dict[str, Any],
        judge: Optional[Dict[str, Any]] = None,
        priority: str = "medium",
        source: str = "quality_gate",
    ) -> Dict[str, Any]:
        """Create or return an open review for the same incident/source."""
        for review in self._reviews:
            if (
                review.get("incident_id") == incident_id
                and review.get("source") == source
                and review.get("status") == "pending"
            ):
                return review

        review = {
            "review_id": f"hitl-{uuid.uuid4().hex[:8]}",
            "incident_id": incident_id,
            "source": source,
            "reason": reason,
            "priority": priority,
            "status": "pending",
            "created_at": time.time(),
            "updated_at": time.time(),
            "rca_result": rca_result,
            "judge": judge or {},
            "decision": "",
            "reviewer": "",
            "expert_diagnosis": "",
            "comment": "",
            "learning_result": {},
        }
        self._reviews.append(review)
        self._save()
        return review

    def list_reviews(self, status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        """List reviews, newest first."""
        items = self._reviews
        if status:
            items = [r for r in items if r.get("status") == status]
        return sorted(items, key=lambda r: r.get("created_at", 0), reverse=True)[:limit]

    def decide_review(
        self,
        review_id: str,
        decision: str,
        reviewer: str = "",
        expert_diagnosis: str = "",
        comment: str = "",
        context_learner=None,
        feedback_store=None,
        trace_store=None,
        fault_store=None,
    ) -> Dict[str, Any]:
        """Record a human decision and optionally trigger supervised learning."""
        decision = (decision or "").strip().lower()
        if decision not in {"approve", "reject", "needs_more_evidence"}:
            raise ValueError("decision must be approve, reject, or needs_more_evidence")

        for review in self._reviews:
            if review.get("review_id") != review_id:
                continue

            review["decision"] = decision
            review["reviewer"] = reviewer
            review["expert_diagnosis"] = expert_diagnosis
            review["comment"] = comment
            review["updated_at"] = time.time()
            review["status"] = "closed" if decision in {"approve", "reject"} else "pending"

            learning_result = {}
            if expert_diagnosis and context_learner is not None:
                if feedback_store is not None:
                    learning_result = feedback_store.submit_feedback(
                        incident_id=review.get("incident_id", review_id),
                        expert_diagnosis=expert_diagnosis,
                        comment=comment,
                        context_learner=context_learner,
                        trace_store=trace_store,
                        fault_store=fault_store,
                    )
                else:
                    learning_result = context_learner.learn_supervised(
                        agent_diagnosis=str(review.get("rca_result", {}))[:2000],
                        ground_truth=expert_diagnosis,
                    )
                review["learning_result"] = learning_result

            self._save()
            return review

        raise KeyError(f"review not found: {review_id}")

    def stats(self) -> Dict[str, Any]:
        total = len(self._reviews)
        pending = sum(1 for r in self._reviews if r.get("status") == "pending")
        closed = sum(1 for r in self._reviews if r.get("status") == "closed")
        learned = sum(1 for r in self._reviews if r.get("learning_result"))
        return {
            "total": total,
            "pending": pending,
            "closed": closed,
            "learned": learned,
        }
