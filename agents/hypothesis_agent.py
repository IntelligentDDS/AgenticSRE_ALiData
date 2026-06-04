"""
AgenticSRE Hypothesis Agent
Generates and re-ranks root cause hypotheses using LLM with historical context injection.
"""

import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


@dataclass
class Hypothesis:
    """A root cause hypothesis with confidence and evidence tracking."""
    id: str
    description: str
    confidence: float = 0.5
    status: str = "active"       # active | confirmed | rejected
    service: str = ""
    fault_type: str = ""
    supporting_evidence: List[str] = field(default_factory=list)
    contradicting_evidence: List[str] = field(default_factory=list)
    investigation_plan: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "description": self.description,
            "confidence": self.confidence,
            "status": self.status,
            "service": self.service,
            "fault_type": self.fault_type,
            "supporting_evidence": self.supporting_evidence,
            "contradicting_evidence": self.contradicting_evidence,
        }


class HypothesisAgent:
    """
    Hypothesis generation and re-ranking agent.
    
    Implements the "Hypothesis" phase of the Discovery→Hypothesis→Plan→Investigate→Reason paradigm.
    Uses historical context from memory to bootstrap hypothesis generation.
    """

    GENERATION_PROMPT = """You are an expert SRE performing root cause analysis on a Kubernetes cluster.

Given the incident signal and any historical context, generate 3-5 ranked root cause hypotheses.

Incident Signal:
{incident_signal}

{historical_context}

For each hypothesis, provide:
1. A specific root cause description
2. Initial confidence (0.0-1.0)
3. The likely affected service/component
4. The fault type category
5. An investigation plan (specific commands/queries to run)

Respond in JSON:
{{
    "hypotheses": [
        {{
            "id": "H1",
            "description": "specific root cause explanation",
            "confidence": 0.7,
            "service": "affected_service",
            "fault_type": "cpu_throttling|memory_leak|network_partition|config_error|dependency_failure|...",
            "investigation_plan": ["kubectl get pods -n ...", "check prometheus metric ..."]
        }}
    ]
}}"""

    RERANK_PROMPT = """You are an expert SRE re-evaluating root cause hypotheses based on new evidence.

Current Hypotheses:
{hypotheses}

New Evidence:
{evidence}

For each hypothesis, update the confidence based on the evidence.
- Increase confidence if evidence supports the hypothesis
- Decrease confidence if evidence contradicts it
- Add the evidence to supporting or contradicting lists

Respond in JSON:
{{
    "hypotheses": [
        {{
            "id": "H1",
            "confidence": 0.85,
            "status": "active|confirmed|rejected",
            "supporting_evidence": ["evidence that supports"],
            "contradicting_evidence": ["evidence that contradicts"],
            "reasoning": "why confidence changed"
        }}
    ]
}}"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def generate(self, incident_signal: str,
                 historical_rules: List[str] = None,
                 historical_faults: List[Dict] = None) -> List[Hypothesis]:
        """Generate initial hypotheses from incident signal + historical context."""
        fast = self._fast_generate(incident_signal)
        if fast:
            return fast
        
        # Build historical context
        hist_context = ""
        if historical_rules:
            hist_context += "\nHistorical diagnostic rules (from past incidents):\n"
            for rule in historical_rules[:5]:
                hist_context += f"  - {rule}\n"
        if historical_faults:
            hist_context += "\nSimilar past incidents:\n"
            for fault in historical_faults[:3]:
                hist_context += f"  - [{fault.get('fault_type', '')}] {fault.get('description', '')[:200]}\n"
                hist_context += f"    Root cause: {fault.get('root_cause', '')[:200]}\n"

        try:
            result = self.llm.json_chat([
                {"role": "system", "content": "You are an expert SRE root cause analyst."},
                {"role": "user", "content": self.GENERATION_PROMPT.format(
                    incident_signal=incident_signal[:3000],
                    historical_context=hist_context if hist_context else "No historical context available.",
                )}
            ])

            hypotheses = []
            for h in result.get("hypotheses", []):
                hypotheses.append(Hypothesis(
                    id=h.get("id", f"H{len(hypotheses)+1}"),
                    description=h.get("description", ""),
                    confidence=min(max(h.get("confidence", 0.5), 0.0), 1.0),
                    service=h.get("service", ""),
                    fault_type=h.get("fault_type", ""),
                    investigation_plan=h.get("investigation_plan", []),
                ))
            
            # Sort by confidence
            hypotheses.sort(key=lambda h: h.confidence, reverse=True)
            return hypotheses

        except Exception as e:
            logger.error(f"Hypothesis generation failed: {e}")
            return [Hypothesis(
                id="H1",
                description=f"General investigation needed for: {incident_signal[:200]}",
                confidence=0.3,
            )]

    def _fast_generate(self, incident_signal: str) -> List[Hypothesis]:
        """Generate deterministic hypotheses for common high-signal K8s incidents."""
        text = incident_signal.lower()
        if any(h in text for h in [
            "failed to pull image", "imagepull", "image pull", "errimagepull",
            "imagepullbackoff", "failed to resolve reference",
        ]):
            return [
                Hypothesis(
                    id="H1",
                    description="Node or cluster egress connectivity to the container registry is failing, causing image manifest resolution or layer download to fail.",
                    confidence=0.86,
                    fault_type="network_registry_access",
                    investigation_plan=[
                        "kubectl describe pod <pod> -n <namespace>",
                        "kubectl get events -n <namespace> --field-selector type=Warning",
                        "check node DNS and HTTPS connectivity to registry-1.docker.io",
                    ],
                ),
                Hypothesis(
                    id="H2",
                    description="DNS resolution or CoreDNS path is unstable for docker.io / registry-1.docker.io.",
                    confidence=0.55,
                    fault_type="dns_failure",
                    investigation_plan=[
                        "check CoreDNS pod health and request metrics",
                        "test registry hostname resolution from affected node",
                    ],
                ),
                Hypothesis(
                    id="H3",
                    description="The image reference or registry availability/rate limit is causing Docker Hub manifest lookup to fail.",
                    confidence=0.35,
                    fault_type="image_registry_or_tag",
                    investigation_plan=[
                        "verify image exists and tag is valid",
                        "check Docker Hub availability and rate limiting",
                    ],
                ),
            ]
        if "crashloopbackoff" in text:
            return [
                Hypothesis(
                    id="H1",
                    description="The container process is repeatedly exiting due to application startup failure, bad configuration, or missing dependency.",
                    confidence=0.8,
                    fault_type="application_crash",
                    investigation_plan=[
                        "kubectl logs --previous for the pod",
                        "kubectl describe pod for last termination state",
                    ],
                ),
                Hypothesis(
                    id="H2",
                    description="Resource limits, probes, or dependency readiness are causing repeated restarts.",
                    confidence=0.5,
                    fault_type="resource_or_probe_failure",
                    investigation_plan=[
                        "check restart reason, probes, and memory limits",
                        "check dependency service readiness",
                    ],
                ),
            ]
        if "pending" in text or "failedscheduling" in text:
            return [
                Hypothesis(
                    id="H1",
                    description="The pod is unschedulable due to insufficient resources, node selectors, taints, PVC binding, or affinity constraints.",
                    confidence=0.8,
                    fault_type="scheduling_failure",
                    investigation_plan=[
                        "kubectl describe pod for scheduling events",
                        "kubectl describe nodes for allocatable resources and taints",
                    ],
                )
            ]
        return []

    def rerank(self, hypotheses: List[Hypothesis], new_evidence: Dict) -> List[Hypothesis]:
        """Re-rank hypotheses based on new evidence from agents."""
        if not hypotheses:
            return hypotheses
        
        # Format current hypotheses
        hyp_text = "\n".join([
            f"[{h.id}] (conf={h.confidence:.2f}) {h.description} "
            f"[supporting: {len(h.supporting_evidence)}, contradicting: {len(h.contradicting_evidence)}]"
            for h in hypotheses
        ])

        # Format new evidence
        evidence_text = ""
        for agent_name, result in new_evidence.items():
            summary = result.get("summary", str(result))[:1000]
            evidence_text += f"\n[{agent_name}]: {summary}\n"

        try:
            result = self.llm.json_chat([
                {"role": "system", "content": "You are an expert SRE evaluating root cause hypotheses."},
                {"role": "user", "content": self.RERANK_PROMPT.format(
                    hypotheses=hyp_text,
                    evidence=evidence_text[:5000],
                )}
            ])

            # Update hypotheses
            updates = {h["id"]: h for h in result.get("hypotheses", [])}
            for hyp in hypotheses:
                if hyp.id in updates:
                    u = updates[hyp.id]
                    hyp.confidence = min(max(u.get("confidence", hyp.confidence), 0.0), 1.0)
                    hyp.status = u.get("status", hyp.status)
                    hyp.supporting_evidence.extend(u.get("supporting_evidence", []))
                    hyp.contradicting_evidence.extend(u.get("contradicting_evidence", []))

            # Re-sort by confidence
            hypotheses.sort(key=lambda h: h.confidence, reverse=True)
            return hypotheses

        except Exception as e:
            logger.error(f"Hypothesis reranking failed: {e}")
            return hypotheses
