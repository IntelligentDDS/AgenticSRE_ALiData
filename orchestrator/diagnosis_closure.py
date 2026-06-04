"""
AgenticSRE Diagnosis Closure Manager

Closes the loop after an RCA result:
quality check -> failure classification -> targeted re-investigation ->
revised RCA -> re-judge/HITL/learning.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ClosurePlan:
    """A concrete plan for closing a weak diagnosis."""

    should_iterate: bool = False
    reason: str = ""
    failure_modes: List[str] = field(default_factory=list)
    target_agents: List[str] = field(default_factory=list)
    focus_queries: Dict[str, str] = field(default_factory=dict)
    max_rounds: int = 1
    hitl_if_unresolved: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DiagnosisClosureManager:
    """Turns diagnosis quality gaps into targeted follow-up work."""

    AGENT_ORDER = ["metric_agent", "log_agent", "trace_agent", "event_agent"]

    def __init__(self, llm=None):
        self.llm = llm

    def build_plan(
        self,
        rca_result: Dict[str, Any],
        judge_result: Dict[str, Any],
        critique: Optional[Dict[str, Any]],
        evidence: Dict[str, Any],
    ) -> ClosurePlan:
        """Classify RCA gaps and decide whether to run a targeted closure round."""
        critique = critique or {}
        score = self._to_float(judge_result.get("combined_score", 0))
        critic_score = self._to_float(critique.get("quality_score", 1))
        needs_review = bool(judge_result.get("needs_review")) or bool(critique.get("needs_revision"))

        missing = [
            a for a in critique.get("missing_evidence", [])
            if a in self.AGENT_ORDER
        ]
        for agent in self.AGENT_ORDER:
            item = evidence.get(agent)
            if not item or item.get("error"):
                missing.append(agent)

        modes = self._classify_failure_modes(rca_result, judge_result, critique, evidence)
        target_agents = self._target_agents(modes, missing)

        should_iterate = needs_review or score < 0.65 or critic_score < 0.72
        if not should_iterate:
            return ClosurePlan(should_iterate=False, reason="RCA quality is sufficient.")

        if not target_agents:
            target_agents = ["metric_agent", "event_agent"]

        return ClosurePlan(
            should_iterate=True,
            reason=self._reason(score, critic_score, modes),
            failure_modes=modes,
            target_agents=target_agents,
            focus_queries={agent: self._focus_query(agent, modes, rca_result, critique) for agent in target_agents},
            max_rounds=1,
            hitl_if_unresolved=True,
        )

    def revise_report(
        self,
        incident_query: str,
        original_result: Dict[str, Any],
        original_evidence: Dict[str, Any],
        closure_evidence: Dict[str, Any],
        closure_plan: ClosurePlan,
    ) -> Dict[str, Any]:
        """Produce a revised RCA using closure evidence."""
        if not self.llm:
            return self._heuristic_revision(original_result, closure_evidence, closure_plan)

        prompt = f"""You are an expert SRE performing a second-pass RCA after quality gates found weaknesses.
Use the new targeted evidence to correct the diagnosis. Do not invent evidence.
If the new evidence does not resolve the weakness, lower confidence and state what remains uncertain.

Incident:
{incident_query[:1500]}

Original RCA:
{str(original_result)[:3500]}

Closure Plan:
{closure_plan.to_dict()}

Original Evidence:
{self._format_evidence(original_evidence, limit=2500)}

Targeted Closure Evidence:
{self._format_evidence(closure_evidence, limit=3500)}

Return JSON:
{{
  "root_cause": "specific corrected root cause",
  "confidence": 0.0,
  "fault_type": "canonical category if possible",
  "affected_services": ["svc"],
  "timeline": [],
  "evidence_summary": {{"metrics": "...", "logs": "...", "traces": "...", "events": "..."}},
  "reasoning_chain": "explain how the second-pass evidence changes or supports the conclusion",
  "remediation_suggestion": "recommended fix",
  "prevention": "recurrence prevention",
  "closure_applied": true
}}"""
        try:
            revised = self.llm.json_chat([
                {"role": "system", "content": "You are an SRE RCA closure agent. Respond with valid JSON only."},
                {"role": "user", "content": prompt},
            ])
            if isinstance(revised, dict) and revised.get("root_cause"):
                revised["closure_applied"] = True
                revised["closure_failure_modes"] = closure_plan.failure_modes
                return self._guard_revision(original_result, revised, closure_plan)
        except Exception as e:
            logger.warning("[DiagnosisClosure] revise_report failed: %s", e)

        return self._heuristic_revision(original_result, closure_evidence, closure_plan)

    def _classify_failure_modes(
        self,
        rca_result: Dict[str, Any],
        judge_result: Dict[str, Any],
        critique: Dict[str, Any],
        evidence: Dict[str, Any],
    ) -> List[str]:
        text = " ".join([
            str(rca_result.get("root_cause", "")),
            str(rca_result.get("fault_type", "")),
            str(rca_result.get("reasoning_chain", "")),
            " ".join(critique.get("weaknesses", []) or []),
            " ".join(critique.get("alternative_hypotheses", []) or []),
            str(judge_result.get("llm_based", "")),
        ]).lower()

        modes: List[str] = []
        if any(a not in evidence or evidence.get(a, {}).get("error") for a in self.AGENT_ORDER):
            modes.append("missing_signal")
        if re.search(r"lack|missing|no specific|generic|insufficient evidence|unsupported", text):
            modes.append("insufficient_evidence")
        if re.search(r"alternative|rule out|competing|not explicitly rule", text):
            modes.append("unresolved_competing_hypotheses")
        if re.search(r"fault.?type|category|software bug|runaway|infinite loop", text):
            modes.append("fault_type_ambiguity")
        if re.search(r"node|pod|service|component|wrong target|blast radius", text):
            modes.append("scope_or_target_ambiguity")
        if re.search(r"timeline|temporal|time window|recent", text):
            modes.append("temporal_ambiguity")
        if not modes and self._to_float(judge_result.get("combined_score", 1)) < 0.65:
            modes.append("low_quality_reasoning")
        return self._dedupe(modes)

    def _target_agents(self, modes: List[str], missing: List[str]) -> List[str]:
        agents: List[str] = list(missing)
        if "insufficient_evidence" in modes or "fault_type_ambiguity" in modes:
            agents.extend(["metric_agent", "event_agent"])
        if "unresolved_competing_hypotheses" in modes:
            agents.extend(["metric_agent", "log_agent", "trace_agent", "event_agent"])
        if "scope_or_target_ambiguity" in modes:
            agents.extend(["trace_agent", "event_agent", "metric_agent"])
        if "temporal_ambiguity" in modes:
            agents.extend(["event_agent", "metric_agent"])
        if "low_quality_reasoning" in modes:
            agents.extend(["metric_agent", "event_agent"])
        return [a for a in self._dedupe(agents) if a in self.AGENT_ORDER]

    def _focus_query(
        self,
        agent: str,
        modes: List[str],
        rca_result: Dict[str, Any],
        critique: Dict[str, Any],
    ) -> str:
        root = rca_result.get("root_cause", "")
        fault_type = rca_result.get("fault_type", "")
        weakness = "; ".join((critique.get("weaknesses") or [])[:3])
        base = (
            f"Second-pass RCA closure. Original root cause: {root}. "
            f"Fault type: {fault_type}. Weaknesses: {weakness}. "
            f"Failure modes: {', '.join(modes)}. "
        )
        if agent == "metric_agent":
            return base + (
                "Collect quantitative resource, latency, saturation, throttling, and time-window evidence. "
                "Use the most recent 5-15 minute window and prioritize target services named in the incident/root cause. "
                "For Kubernetes CPU issues, explicitly check container CPU usage, CPU throttling, CPU quota/limits, pod-to-node placement, and node CPU. "
                "Distinguish resource exhaustion from software bug and dependency failure."
            )
        if agent == "log_agent":
            return base + "Search for concrete error lines, exception signatures, timeout messages, and evidence that supports or refutes the claimed mechanism."
        if agent == "trace_agent":
            return base + "Check service-to-service path, bottleneck span, upstream/downstream propagation, and whether the suspected service is the first anomalous hop."
        if agent == "event_agent":
            return base + (
                "Check recent Kubernetes events, pod lifecycle, node readiness, image pulls, scheduling, and whether events are temporally aligned with the incident. "
                "Down-rank stale events that started days ago unless their last-seen time and affected pod placement directly align with the current incident."
            )
        return base

    def _reason(self, score: float, critic_score: float, modes: List[str]) -> str:
        return (
            f"Quality gate requested closure: judge_score={score:.3f}, "
            f"critic_score={critic_score:.3f}, modes={', '.join(modes) or 'unknown'}."
        )

    def _heuristic_revision(
        self,
        original_result: Dict[str, Any],
        closure_evidence: Dict[str, Any],
        closure_plan: ClosurePlan,
    ) -> Dict[str, Any]:
        revised = dict(original_result)
        revised["closure_applied"] = True
        revised["closure_failure_modes"] = closure_plan.failure_modes
        revised["closure_note"] = "Second-pass evidence was collected; LLM revision was unavailable."
        if closure_evidence:
            revised.setdefault("evidence_summary", {})
            if isinstance(revised["evidence_summary"], dict):
                revised["evidence_summary"]["closure"] = self._format_evidence(closure_evidence, limit=600)
        if "insufficient_evidence" in closure_plan.failure_modes:
            conf = self._to_float(revised.get("confidence", 0))
            revised["confidence"] = round(max(0.0, min(1.0, conf - 0.1)), 3)
        return revised

    def _guard_revision(
        self,
        original_result: Dict[str, Any],
        revised: Dict[str, Any],
        closure_plan: ClosurePlan,
    ) -> Dict[str, Any]:
        """
        Prevent a weak second pass from erasing a plausible first-pass RCA.

        Closure may lower confidence when evidence is insufficient, but it should
        not replace a concrete RCA with "unknown" unless the closure evidence
        explicitly contradicts the original conclusion.
        """
        root = str(revised.get("root_cause", "")).lower()
        fault_type = str(revised.get("fault_type", "")).lower()
        says_unknown = (
            fault_type in {"", "unknown"}
            or "insufficient evidence" in root
            or "cannot determine" in root
            or "unknown" == root.strip()
        )
        contradicts = any(
            mode in closure_plan.failure_modes
            for mode in ["unresolved_competing_hypotheses", "scope_or_target_ambiguity"]
        )
        if not says_unknown:
            return revised

        guarded = dict(original_result)
        original_conf = self._to_float(original_result.get("confidence", 0))
        penalty = 0.2 if contradicts else 0.12
        guarded["confidence"] = round(max(0.1, original_conf - penalty), 3)
        guarded["closure_applied"] = True
        guarded["closure_guarded"] = True
        guarded["closure_failure_modes"] = closure_plan.failure_modes
        guarded["closure_note"] = (
            "Second-pass evidence was insufficient to replace the original RCA; "
            "the system preserved the original diagnosis with reduced confidence and routed it to HITL."
        )
        return guarded

    def _format_evidence(self, evidence: Dict[str, Any], limit: int = 3500) -> str:
        parts: List[str] = []
        for agent, result in evidence.items():
            summary = result.get("summary", str(result)) if isinstance(result, dict) else str(result)
            parts.append(f"[{agent}] {summary[:800]}")
        return "\n".join(parts)[:limit]

    def _to_float(self, value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    def _dedupe(self, items: List[str]) -> List[str]:
        seen = set()
        out = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out
