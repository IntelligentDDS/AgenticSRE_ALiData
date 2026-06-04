"""
Plan-and-Execute Paradigm — Hypothesis-driven structured investigation.
Wraps the existing rca_engine logic: Hypothesis → Plan → Parallel Investigate → Re-rank → Correlate → Report.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

from paradigms.base import AgentPool, ParadigmBase, ParadigmMetrics, ParadigmResult
from paradigms.registry import register_paradigm

logger = logging.getLogger(__name__)

FINAL_REPORT_PROMPT = """You are an expert SRE producing the final Root Cause Analysis report.

Incident: {incident}

Hypotheses (ranked by confidence):
{hypotheses}

Evidence from investigation:
{evidence}

Cross-signal correlation:
{correlation}

Produce a comprehensive RCA report in JSON:
{{
    "root_cause": "specific, actionable root cause statement",
    "confidence": 0.0,
    "fault_type": "category of fault",
    "affected_services": ["svc1", "svc2"],
    "evidence_summary": {{
        "metrics": "key metric findings",
        "logs": "key log findings",
        "traces": "key trace findings",
        "events": "key event findings"
    }},
    "reasoning_chain": "step-by-step reasoning from evidence to conclusion",
    "remediation_suggestion": "recommended fix"
}}"""


@register_paradigm
class PlanAndExecuteParadigm(ParadigmBase):
    """
    Hypothesis-driven plan-and-execute: generate hypotheses, plan investigation,
    run domain agents in parallel, re-rank hypotheses, correlate signals, report.
    Re-implements the core rca_engine logic as a paradigm.
    """

    name = "plan_and_execute"
    description = "Hypothesis-driven: generate hypotheses → plan → parallel investigate → re-rank → correlate"

    async def _execute(
        self,
        incident_query: str,
        namespace: str,
        metrics: ParadigmMetrics,
    ) -> ParadigmResult:
        pool = self.pool
        cfg = pool.cfg

        # Build unified context (domain hints + historical rules + feedback)
        context = pool.build_context(incident_query)

        # Step 1: Generate hypotheses (with historical context injection)
        self.log("  [plan_and_execute] Step 1: Generating hypotheses...")
        historical_rules = []
        historical_faults = []
        if context is not None:
            historical_rules = [
                r.get("text", r.get("condition", str(r)))
                for r in context.historical_rules
            ]
            historical_faults = context.similar_faults
        hypotheses = pool.hypothesis_agent.generate(
            incident_query,
            historical_rules=historical_rules,
            historical_faults=historical_faults,
        )
        metrics.llm_calls += 1
        for h in hypotheses:
            self.log(f"    [{h.id}] conf={h.confidence:.2f} — {h.description[:80]}")

        # Step 2: Iterative evidence loop
        max_iter = cfg.pipeline.max_evidence_iterations
        confidence_threshold = cfg.pipeline.hypothesis_confidence_threshold
        all_evidence: Dict[str, Dict] = {}

        for iteration in range(max_iter):
            metrics.iterations = iteration + 1
            self.log(f"  [plan_and_execute] Iteration {iteration + 1}/{max_iter}")

            # Generate plan
            plan = pool.planning_agent.generate_plan(
                [h.to_dict() for h in hypotheses],
                incident_query, iteration
            )
            metrics.llm_calls += 1
            self.log(f"    Plan: {len(plan.get('plan', []))} steps")

            # Run domain agents in parallel (with domain-enriched queries)
            self.log("    Running domain agents in parallel...")
            new_evidence = await pool.run_all_domain_agents_enriched(
                incident_query, namespace, context
            )
            metrics.agent_calls += 4

            for name, result in new_evidence.items():
                summary = result.get("summary", "")[:100]
                self.log(f"    {name}: {summary}")
                all_evidence[name] = result

            # Re-rank hypotheses
            self.log("    Re-ranking hypotheses...")
            hypotheses = pool.hypothesis_agent.rerank(hypotheses, new_evidence)
            metrics.llm_calls += 1
            top = max(hypotheses, key=lambda h: h.confidence) if hypotheses else None
            if top:
                self.log(f"    Top: [{top.id}] conf={top.confidence:.2f} — {top.description[:60]}")

            # Early exit
            if top and top.confidence >= confidence_threshold:
                self.log(f"    High confidence ({top.confidence:.2f}), stopping iterations")
                break

        # Step 3: Cross-signal correlation
        if cfg.pipeline.enable_correlation:
            self.log("  [plan_and_execute] Running cross-signal correlation...")
            correlation_result = pool.correlation_agent.correlate(all_evidence)
            metrics.llm_calls += 1
            all_evidence["correlation"] = correlation_result
            self.log(f"    Top suspect: {correlation_result.get('top_suspect', 'N/A')}")
        else:
            correlation_result = {}

        # Step 4: Final report
        self.log("  [plan_and_execute] Generating final report...")
        hyp_text = "\n".join([
            f"[{h.id}] conf={h.confidence:.2f} — {h.description}"
            for h in hypotheses[:5]
        ])
        evidence_text = ""
        for agent, result in all_evidence.items():
            summary = result.get("summary", str(result))[:500]
            evidence_text += f"\n[{agent}]: {summary}\n"

        report = await pool.llm.async_json_chat([
            {"role": "system", "content": "You are an expert SRE. Respond with valid JSON only."},
            {"role": "user", "content": FINAL_REPORT_PROMPT.format(
                incident=incident_query,
                hypotheses=hyp_text,
                evidence=evidence_text[:6000],
                correlation=str(correlation_result.get("summary", ""))[:2000],
            )},
        ])
        metrics.llm_calls += 1

        return ParadigmResult(
            root_cause=report.get("root_cause", ""),
            confidence=report.get("confidence", 0),
            fault_type=report.get("fault_type", ""),
            affected_services=report.get("affected_services", []),
            evidence_summary=report.get("evidence_summary", {}),
            reasoning_chain=report.get("reasoning_chain", ""),
            remediation_suggestion=report.get("remediation_suggestion", ""),
            raw_output=report,
        )
