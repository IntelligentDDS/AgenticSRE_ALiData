"""
AgenticSRE Collaboration Optimizer

Lightweight production-oriented optimizer for multi-agent RCA orchestration.
It operationalizes ideas from the research report:
- A-Mem / ExpeL: retrieve and compress reusable diagnostic memory.
- OMAC / AgentFlow: choose a collaboration workflow from task and history.
- Reflection / Critic: review the final RCA and trigger targeted refinement.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from memory import AgentContext, ContextBuilder, DomainAdapter

logger = logging.getLogger(__name__)


@dataclass
class CollaborationPolicy:
    """Runtime policy for one RCA session."""

    strategy: str = "plan_and_execute"
    rationale: str = ""
    max_iterations: int = 3
    confidence_threshold: float = 0.85
    agents: List[str] = field(default_factory=lambda: [
        "metric_agent", "log_agent", "trace_agent", "event_agent"
    ])
    require_reflection: bool = True
    reflection_rounds: int = 1
    require_human_review: bool = False
    memory_mode: str = "enriched"
    evidence_focus: List[str] = field(default_factory=list)
    sow_methods: List[str] = field(default_factory=lambda: [
        "A-Mem/ExpeL memory injection",
        "OMAC-style dynamic team selection",
        "Reflection critic quality gate",
    ])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CollaborationOptimizer:
    """
    Selects and applies multi-agent collaboration policy.

    This class intentionally avoids replacing existing agents. It optimizes
    orchestration around them so the Web UI, daemon, and evaluation runners keep
    using the same RCA entry point.
    """

    _FAULT_HINTS = {
        "cpu": ["cpu", "throttl", "stress", "load", "saturat", "高负载", "CPU"],
        "memory": ["memory", "oom", "mem", "内存", "OOMKilled"],
        "network": ["network", "dns", "timeout", "latency", "连接", "超时"],
        "k8s": ["CrashLoopBackOff", "ImagePull", "NodeNotReady", "Evicted", "pod", "node"],
        "dependency": ["redis", "mongo", "mysql", "dependency", "downstream", "上游", "下游"],
        "llm_inference": [
            "vllm", "llm", "gpu", "cuda", "nccl", "kv cache", "ttft", "tpot",
            "tokenizer", "safetensor", "inference", "推理", "大模型", "显存", "智算",
        ],
    }

    def __init__(
        self,
        cfg,
        fault_store=None,
        feedback_store=None,
        trace_store=None,
        domain_adapter=None,
        llm=None,
    ):
        self.cfg = cfg
        self.fault_store = fault_store
        self.feedback_store = feedback_store
        self.trace_store = trace_store
        self.domain_adapter = domain_adapter or self._safe_domain_adapter()
        self.llm = llm
        self.context_builder = ContextBuilder(
            fault_store=fault_store,
            feedback_store=feedback_store,
            domain_adapter=self.domain_adapter,
            trace_store=trace_store,
        )

    def _safe_domain_adapter(self):
        try:
            return DomainAdapter.from_config()
        except Exception as e:
            logger.debug("DomainAdapter init failed: %s", e)
            return None

    def build_context(self, incident_query: str) -> AgentContext:
        """Build compact reusable context for the RCA session."""
        try:
            return self.context_builder.build_context(incident_query)
        except Exception as e:
            logger.warning("[CollaborationOptimizer] context build failed: %s", e)
            return AgentContext()

    def select_policy(self, incident_query: str, context: Optional[AgentContext] = None) -> CollaborationPolicy:
        """
        Choose a collaboration policy from incident features and recent memory.

        Heuristics are deliberately conservative and explainable; they can later
        be replaced by learned policy search when enough labelled traces exist.
        """
        context = context or AgentContext()
        text = incident_query.lower()
        signals = self._detect_signals(incident_query)
        fault_count = len([k for k, v in signals.items() if v])
        similar_faults = len(context.similar_faults)
        expert_feedback = len(context.expert_feedback)
        recent_traces = context.recent_traces or []

        policy = CollaborationPolicy(
            max_iterations=getattr(self.cfg.pipeline, "max_evidence_iterations", 3),
            confidence_threshold=getattr(self.cfg.pipeline, "hypothesis_confidence_threshold", 0.85),
        )

        if signals.get("llm_inference"):
            policy.strategy = "llm_inference_plan"
            policy.rationale = "Incident targets vLLM/GPU inference runtime; include specialized inference evidence."
            policy.agents = ["llm_inference_agent", "metric_agent", "event_agent", "log_agent"]
            policy.confidence_threshold = min(policy.confidence_threshold, 0.8)
            policy.evidence_focus = ["vLLM runtime metrics", "GPU/CUDA/NCCL symptoms", "model asset and tokenizer state"]
        elif fault_count >= 3 or any(w in text for w in ["multi", "cascade", "级联", "复合", "multiple"]):
            policy.strategy = "debate_reflection"
            policy.rationale = "Incident spans multiple signal families; use bounded debate plus critic review."
            policy.reflection_rounds = 2
            policy.evidence_focus = ["competing hypotheses", "cross-service propagation", "blast radius"]
        elif any(signals[k] for k in ["network", "dependency"]):
            policy.strategy = "react_plan_hybrid"
            policy.rationale = "Network/dependency incidents need adaptive tool selection while preserving auditability."
            policy.confidence_threshold = min(policy.confidence_threshold, 0.8)
            policy.evidence_focus = ["dependency health", "timeouts", "service-to-service correlation"]
        elif similar_faults >= 2 or expert_feedback:
            policy.strategy = "memory_augmented_plan"
            policy.rationale = "Similar memory exists; prioritize memory-guided plan-and-execute."
            policy.evidence_focus = ["memory-matched symptoms", "known remediation checks"]
        else:
            policy.strategy = "plan_and_execute"
            policy.rationale = "Default auditable hypothesis-driven workflow."
            policy.evidence_focus = ["events", "metrics", "logs", "traces"]

        if signals["cpu"]:
            policy.evidence_focus.extend(["CPU saturation", "CFS throttling", "noisy neighbor"])
        if signals["memory"]:
            policy.evidence_focus.extend(["OOMKilled", "working set growth", "memory limits"])
        if signals["k8s"]:
            policy.evidence_focus.extend(["pod lifecycle", "node readiness", "recent Kubernetes events"])
        if signals.get("llm_inference"):
            policy.evidence_focus.extend(["TTFT/TPOT", "KV cache", "request queue", "GPU utilization", "model loading errors"])

        if self._recent_quality_is_weak(recent_traces):
            policy.require_reflection = True
            policy.reflection_rounds = max(policy.reflection_rounds, 2)
            policy.require_human_review = True
            policy.rationale += " Recent quality signals are weak; tighten review gate."

        policy.evidence_focus = self._dedupe(policy.evidence_focus)[:10]
        return policy

    def enrich_incident_query(
        self,
        incident_query: str,
        context: Optional[AgentContext],
        policy: CollaborationPolicy,
        agent_name: str = "",
    ) -> str:
        """Inject compact memory, domain hints, and policy focus into an agent query."""
        if context is None or policy.memory_mode == "off":
            return incident_query

        context_block = context.to_context_string(agent_name=agent_name)
        policy_block = self._policy_context(policy, agent_name=agent_name)
        parts = [p for p in [context_block, policy_block] if p]
        if not parts:
            return incident_query
        return "\n\n".join(parts + [f"# Current Incident\n{incident_query}"])

    def critique_report(
        self,
        incident_query: str,
        final_result: Dict[str, Any],
        evidence: Dict[str, Any],
        policy: CollaborationPolicy,
    ) -> Dict[str, Any]:
        """Run a bounded critic pass over the final RCA report."""
        fallback = self._heuristic_critique(final_result, evidence, policy)
        if not self.llm or not policy.require_reflection:
            return fallback

        prompt = f"""You are a strict SRE Critic Agent.
Review this RCA for evidence grounding, specificity, fault-type accuracy, and operational usefulness.

Incident:
{incident_query[:1500]}

Collaboration Policy:
{policy.to_dict()}

RCA Report:
{str(final_result)[:3500]}

Evidence Summaries:
{self._format_evidence(evidence, limit=3500)}

Respond in JSON:
{{
  "quality_score": 0.0,
  "needs_revision": true,
  "needs_human_review": false,
  "weaknesses": ["..."],
  "missing_evidence": ["..."],
  "alternative_hypotheses": ["..."],
  "confidence_adjustment": -0.1,
  "recommended_focus": ["..."]
}}"""
        try:
            critique = self.llm.json_chat([
                {"role": "system", "content": "You are an SRE RCA critic. Respond with valid JSON only."},
                {"role": "user", "content": prompt},
            ])
            if not isinstance(critique, dict):
                return fallback
            for key, value in fallback.items():
                critique.setdefault(key, value)
            return critique
        except Exception as e:
            logger.warning("[CollaborationOptimizer] critic failed: %s", e)
            return fallback

    def improve_report(
        self,
        incident_query: str,
        final_result: Dict[str, Any],
        evidence: Dict[str, Any],
        critique: Dict[str, Any],
        policy: CollaborationPolicy,
    ) -> Dict[str, Any]:
        """Generate a revised RCA report when critic identifies concrete gaps."""
        if not self.llm or not critique.get("needs_revision"):
            return self._apply_confidence_adjustment(final_result, critique)

        prompt = f"""You are an expert SRE improving a Root Cause Analysis.
Use the critic feedback to make the report more specific and evidence-grounded.
Do not invent evidence. Lower confidence when evidence is indirect or conflicting.

Incident:
{incident_query[:1500]}

Original RCA:
{str(final_result)[:3500]}

Critic Feedback:
{str(critique)[:2500]}

Evidence Summaries:
{self._format_evidence(evidence, limit=3500)}

Return the same JSON schema as the original RCA report:
{{
  "root_cause": "specific, actionable root cause statement",
  "confidence": 0.0,
  "fault_type": "category",
  "affected_services": ["svc"],
  "timeline": [],
  "evidence_summary": {{"metrics": "...", "logs": "...", "traces": "...", "events": "..."}},
  "reasoning_chain": "step-by-step evidence grounded reasoning",
  "remediation_suggestion": "recommended fix",
  "prevention": "recurrence prevention"
}}"""
        try:
            improved = self.llm.json_chat([
                {"role": "system", "content": "You are an expert SRE RCA writer. Respond with valid JSON only."},
                {"role": "user", "content": prompt},
            ])
            if isinstance(improved, dict) and improved.get("root_cause"):
                improved["collaboration_refined"] = True
                return self._apply_confidence_adjustment(improved, critique)
        except Exception as e:
            logger.warning("[CollaborationOptimizer] report improvement failed: %s", e)

        return self._apply_confidence_adjustment(final_result, critique)

    def calibrate_fault_type(
        self,
        final_result: Dict[str, Any],
        evidence: Dict[str, Any],
        policy: CollaborationPolicy,
    ) -> Dict[str, Any]:
        """
        Normalize free-form LLM fault types to the operational taxonomy used by
        evaluation, dashboards, and SOW reporting.

        This preserves the detailed root cause while making the category
        comparable across incidents and paradigms.
        """
        adjusted = dict(final_result)
        original = str(adjusted.get("fault_type", "") or "")
        text = " ".join([
            str(adjusted.get("root_cause", "")),
            original,
            str(adjusted.get("reasoning_chain", "")),
            str(adjusted.get("evidence_summary", "")),
            " ".join(policy.evidence_focus or []),
            self._format_evidence(evidence, limit=2500),
        ]).lower()

        mapped = self._map_fault_type(text, original)
        if mapped:
            adjusted["fault_type"] = mapped
            if original and original.lower() != mapped.lower():
                adjusted["fault_type_original"] = original
            adjusted["fault_type_reason"] = self._fault_type_reason(mapped, text)
        return adjusted

    def _detect_signals(self, incident_query: str) -> Dict[str, bool]:
        return {
            name: any(h.lower() in incident_query.lower() for h in hints)
            for name, hints in self._FAULT_HINTS.items()
        }

    def _map_fault_type(self, text: str, original: str = "") -> str:
        original_l = original.lower()
        canonical = {
            "resource_exhaustion",
            "dependency_failure",
            "network_issue",
            "service_disruption",
            "application_crash",
            "configuration_error",
            "infrastructure",
            "compound_failure",
            "unknown",
        }
        if original_l in canonical:
            return original_l

        if any(k in text for k in [
            "cpu saturation", "cpu saturat", "cpu pressure", "cpu thrott", "cfs thrott",
            "resource exhaustion", "resource pressure", "memory pressure", "oom", "oomkilled",
            "quota", "insufficient resource", "noisy neighbor", "runaway process", "infinite loop",
        ]):
            return "resource_exhaustion"
        if any(k in text for k in [
            "redis", "mongodb", "mongo", "mysql", "postgres", "memcached", "cache",
            "dependency", "downstream", "upstream", "connection refused",
        ]):
            return "dependency_failure"
        if any(k in text for k in [
            "network", "dns", "packet loss", "netem", "latency injection", "connection timeout",
        ]):
            return "network_issue"
        if any(k in text for k in [
            "no ready endpoints", "replicas=0", "scaled to 0", "service unavailable", "503",
        ]):
            return "service_disruption"
        if any(k in text for k in [
            "crashloopbackoff", "crash loop", "exit code", "back-off restarting",
            "liveness probe", "readiness probe",
        ]):
            return "application_crash"
        if any(k in text for k in [
            "imagepull", "errimagepull", "configmap", "secret", "mount", "misconfiguration",
            "invalid configuration",
        ]):
            return "configuration_error"
        if any(k in text for k in [
            "nodenotready", "disk pressure", "node pressure", "kubelet", "container runtime",
            "infrastructure",
        ]):
            return "infrastructure"
        if any(k in text for k in ["compound", "multiple independent", "two independent", "co-occurring"]):
            return "compound_failure"
        return original or "unknown"

    def _fault_type_reason(self, mapped: str, text: str) -> str:
        reasons = {
            "resource_exhaustion": "CPU/memory/resource pressure terms were present in the RCA, evidence, or collaboration focus.",
            "dependency_failure": "Dependency/cache/database/upstream failure terms were present.",
            "network_issue": "Network/DNS/timeout/latency injection terms were present.",
            "service_disruption": "Replica/endpoints/service unavailable terms were present.",
            "application_crash": "CrashLoopBackOff/restart/exit/probe failure terms were present.",
            "configuration_error": "Image/config/secret/mount/misconfiguration terms were present.",
            "infrastructure": "Node/kubelet/runtime/infrastructure pressure terms were present.",
            "compound_failure": "Multiple independent or co-occurring failure terms were present.",
        }
        return reasons.get(mapped, "No strong canonical mapping signal was found.")

    def _recent_quality_is_weak(self, traces: List[Dict[str, Any]]) -> bool:
        if not traces:
            return False
        quality_values: List[float] = []
        for trace in traces[-5:]:
            for key in ("judge_score", "quality_score", "combined_score"):
                val = trace.get(key)
                if isinstance(val, (int, float)) and val > 0:
                    quality_values.append(float(val))
        return bool(quality_values) and sum(quality_values) / len(quality_values) < 0.65

    def _policy_context(self, policy: CollaborationPolicy, agent_name: str = "") -> str:
        focus = "\n".join(f"- {item}" for item in policy.evidence_focus[:8])
        agent_hint = f"\nTarget Agent: {agent_name}" if agent_name else ""
        return (
            "# Collaboration Policy\n"
            f"Strategy: {policy.strategy}\n"
            f"Rationale: {policy.rationale}\n"
            f"Evidence Focus:\n{focus or '- standard multi-signal evidence'}"
            f"{agent_hint}"
        )

    def _heuristic_critique(
        self,
        final_result: Dict[str, Any],
        evidence: Dict[str, Any],
        policy: CollaborationPolicy,
    ) -> Dict[str, Any]:
        weaknesses: List[str] = []
        missing: List[str] = []
        confidence = self._to_float(final_result.get("confidence", 0))
        root_cause = str(final_result.get("root_cause", ""))
        fault_type = str(final_result.get("fault_type", ""))

        if len(root_cause.strip()) < 30 or re.search(r"\bunknown|general investigation|manual\b", root_cause, re.I):
            weaknesses.append("Root cause is too vague for actionable SRE remediation.")
        if confidence >= 0.85 and len(evidence) < 3:
            weaknesses.append("High confidence is not backed by enough independent evidence sources.")
        if not fault_type or fault_type.lower() == "unknown":
            weaknesses.append("Fault type is missing or unknown.")

        for agent in ("metric_agent", "log_agent", "trace_agent", "event_agent"):
            item = evidence.get(agent)
            if not item or item.get("error"):
                missing.append(agent)

        score = 0.9 - 0.12 * len(weaknesses) - 0.04 * len(missing)
        score = max(0.0, min(1.0, score))
        return {
            "quality_score": score,
            "needs_revision": score < 0.82 or bool(weaknesses),
            "needs_human_review": policy.require_human_review or score < 0.65,
            "weaknesses": weaknesses,
            "missing_evidence": missing,
            "alternative_hypotheses": [],
            "confidence_adjustment": -0.1 if weaknesses else 0.0,
            "recommended_focus": policy.evidence_focus[:5],
        }

    def _apply_confidence_adjustment(self, result: Dict[str, Any], critique: Dict[str, Any]) -> Dict[str, Any]:
        adjusted = dict(result)
        delta = self._to_float(critique.get("confidence_adjustment", 0))
        if delta:
            conf = self._to_float(adjusted.get("confidence", 0))
            adjusted["confidence"] = round(max(0.0, min(1.0, conf + delta)), 3)
        adjusted["collaboration_refined"] = adjusted.get("collaboration_refined", False)
        return adjusted

    def _format_evidence(self, evidence: Dict[str, Any], limit: int = 3500) -> str:
        parts: List[str] = []
        for agent, result in evidence.items():
            if isinstance(result, dict):
                summary = result.get("summary", str(result))
            else:
                summary = str(result)
            parts.append(f"[{agent}] {summary[:700]}")
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
            key = item.lower()
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out
