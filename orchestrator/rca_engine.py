"""
AgenticSRE RCA Engine
Core hypothesis-driven RCA loop: the heart of the system.
Implements: Discovery → Hypothesis → Plan → Investigate → Reason
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from configs.config_loader import get_config
from tools import build_tool_registry, LLMClient, ToolRegistry
from agents import (
    MetricAgent, LogAgent, TraceAgent, EventAgent,
    HypothesisAgent, CorrelationAgent, PlanningAgent,
    RemediationAgent, AlertAgent, ProfilingAgent, LLMInferenceAgent,
)
from memory import (
    FaultContextStore, ContextLearner, RCAJudge, TraceStore, AgentTrace,
    ExpertFeedbackStore, EvolutionTracker, HumanReviewStore,
)
from observability import AgentTracer, MetricsCollector
from orchestrator.collaboration_optimizer import CollaborationOptimizer, CollaborationPolicy
from orchestrator.diagnosis_closure import DiagnosisClosureManager
from orchestrator.session import RCASession

logger = logging.getLogger(__name__)


def _agent_query(
    incident_query: str,
    agent_context,
    collaboration_policy: CollaborationPolicy,
    collaboration_optimizer: Optional[CollaborationOptimizer],
    agent_name: str,
) -> str:
    """Build the per-agent query, falling back to the raw incident when disabled."""
    if collaboration_optimizer is None:
        return incident_query
    return collaboration_optimizer.enrich_incident_query(
        incident_query, agent_context, collaboration_policy, agent_name
    )


def _is_k8s_lifecycle_incident(incident_query: str) -> bool:
    """True for incidents where K8s events are the primary evidence source."""
    text = incident_query.lower()
    hints = [
        "imagepull", "image pull", "errimagepull", "imagepullbackoff",
        "failed to pull image", "failed to resolve reference",
        "crashloopbackoff", "createcontainer", "pending", "failedscheduling",
        "pod/", "pod ", "containercreating",
    ]
    return any(h in text for h in hints)


def _is_llm_inference_incident(incident_query: str) -> bool:
    """True when incident likely targets vLLM/GPU inference runtime."""
    text = incident_query.lower()
    hints = [
        "vllm", "llm", "gpu", "cuda", "nccl", "kv cache", "kv-cache",
        "ttft", "tpot", "token", "tokenizer", "safetensor", "model weight",
        "inference", "openai", "推理", "大模型", "显存", "智算",
    ]
    return any(h in text for h in hints)


def _selected_evidence_agents(
    incident_query: str,
    cfg,
    policy: Optional[CollaborationPolicy] = None,
) -> List[str]:
    """Select a minimal useful evidence set for common incident classes."""
    if not getattr(cfg.pipeline, "enable_fast_evidence_selection", True):
        selected = ["metric_agent", "log_agent", "trace_agent", "event_agent"]
    else:
        text = incident_query.lower()
        if _is_llm_inference_incident(incident_query) and getattr(cfg.pipeline, "enable_llm_inference_agent", True):
            selected = ["llm_inference_agent", "metric_agent", "event_agent", "log_agent"]
        elif _is_k8s_lifecycle_incident(incident_query):
            # ImagePull/Pending/CrashLoop evidence is usually in events, pod status,
            # and node/CoreDNS metrics. Logs/traces are often empty or unrelated.
            selected = ["event_agent", "metric_agent"]
        elif any(h in text for h in ["dns", "network", "timeout", "connection", "连接", "超时"]):
            selected = ["event_agent", "metric_agent", "trace_agent"]
        elif any(h in text for h in ["latency", "p99", "5xx", "http", "service"]):
            selected = ["metric_agent", "trace_agent", "event_agent"]
        else:
            selected = ["metric_agent", "event_agent"]

    if policy and policy.agents:
        allowed = {
            "metric_agent", "log_agent", "trace_agent", "event_agent", "llm_inference_agent"
        }
        policy_agents = [a for a in policy.agents if a in allowed]
        if policy_agents:
            selected = list(dict.fromkeys(selected + policy_agents))
    return selected


def _extract_evidence_items(evidence: Dict[str, Dict]) -> List[Dict[str, Any]]:
    """Flatten normalized evidence_items from agent outputs."""
    items: List[Dict[str, Any]] = []
    for agent_name, result in evidence.items():
        if not isinstance(result, dict):
            continue
        for item in result.get("evidence_items", []) or []:
            if isinstance(item, dict):
                enriched = dict(item)
                enriched.setdefault("source_agent", agent_name)
                items.append(enriched)
    return items[:100]


async def _run_agent_with_timeout(name: str, coro, timeout_s: int) -> Any:
    """Run one evidence agent with a bounded timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=max(1, timeout_s))
    except asyncio.TimeoutError:
        raise TimeoutError(f"{name} exceeded {timeout_s}s evidence timeout")


# ── Final LLM Report Prompt ──

FINAL_REPORT_PROMPT = """You are an expert SRE producing the final Root Cause Analysis report.

Incident: {incident}

Hypotheses (ranked by confidence):
{hypotheses}

Evidence from investigation:
{evidence}

Cross-signal correlation:
{correlation}

Produce a comprehensive, structured RCA report in JSON:
{{
    "root_cause": "specific, actionable root cause statement",
    "confidence": 0.85,
    "fault_type": "category of fault",
    "affected_services": ["svc1", "svc2"],
    "timeline": [
        {{"time": "approximate time", "event": "what happened"}}
    ],
    "evidence_summary": {{
        "metrics": "key metric findings",
        "logs": "key log findings",
        "traces": "key trace findings",
        "events": "key event findings"
    }},
    "reasoning_chain": "step-by-step reasoning from evidence to conclusion",
    "remediation_suggestion": "recommended fix",
    "prevention": "how to prevent recurrence"
}}"""


async def run_rca(
    incident_query: str,
    namespace: str = "",
    config=None,
    log_callback: Optional[Callable] = None,
    registry: Optional[ToolRegistry] = None,
) -> Dict:
    """
    Execute the full hypothesis-driven RCA pipeline.
    
    Flow:
    1. Build ToolRegistry → Init Memory → Historical Context
    2. Generate hypotheses (with historical injection)
    3. Iterative evidence loop (Metric/Log/Trace/Event agents in parallel)
    4. Cross-signal correlation
    5. Graph-based RCA localization
    6. Final LLM report
    7. Quality judge → Auto-learning
    8. Optional self-healing
    """
    cfg = config or get_config()
    session_id = f"rca-{uuid.uuid4().hex[:8]}"
    session = RCASession(
        session_id=session_id,
        incident_query=incident_query,
        namespace=namespace,
        status="running",
    )

    def log(msg: str):
        session.log(msg)
        logger.info(msg)
        if log_callback:
            log_callback(msg)

    def emit(event: dict):
        """Emit structured event through log_callback."""
        if log_callback:
            log_callback(event)

    try:
        # ── Step 0: Setup ──
        log("🔧 Initializing tools and agents...")
        if registry is None:
            registry = build_tool_registry(cfg, allow_write=cfg.runtime.enable_self_healing)
        
        llm = LLMClient(cfg.llm)
        
        # Init agents
        metric_agent = MetricAgent(llm, registry)
        log_agent = LogAgent(llm, registry)
        trace_agent = TraceAgent(llm, registry)
        event_agent = EventAgent(llm, registry)
        llm_inference_agent = LLMInferenceAgent(llm, registry)
        evidence_summary_max_tokens = getattr(cfg.pipeline, "evidence_summary_max_tokens", 1024)
        for agent in (metric_agent, log_agent, trace_agent, event_agent, llm_inference_agent):
            agent.summary_max_tokens = evidence_summary_max_tokens
        hypothesis_agent = HypothesisAgent(llm)
        correlation_agent = CorrelationAgent(llm)
        planning_agent = PlanningAgent(llm, registry)

        # Init memory
        store = FaultContextStore(cfg) if cfg.memory.enabled else None
        learner = ContextLearner(llm, store, cfg) if store else None
        judge = RCAJudge(llm, cfg)
        trace_store = TraceStore(cfg)
        feedback_store = ExpertFeedbackStore()
        evolution_tracker = EvolutionTracker.from_config() if cfg.evolution.enabled else None
        review_store = HumanReviewStore()
        closure_manager = DiagnosisClosureManager(llm=llm)
        collaboration_optimizer = None
        if cfg.pipeline.enable_collaboration_optimization:
            collaboration_optimizer = CollaborationOptimizer(
                cfg=cfg,
                fault_store=store,
                feedback_store=feedback_store,
                trace_store=trace_store,
                llm=llm,
            )
        
        # Start pipeline trace
        pipe_trace = trace_store.start_pipeline(session_id, incident_query)

        # ── Step 1: Historical Context ──
        session.add_phase(1, "CONTEXT_RETRIEVAL")
        emit({"event": "phase_start", "phase": 1, "name": "CONTEXT_RETRIEVAL"})
        log("📚 Retrieving historical context...")
        if collaboration_optimizer is not None:
            agent_context = collaboration_optimizer.build_context(incident_query)
            collaboration_policy = collaboration_optimizer.select_policy(incident_query, agent_context)
            enriched_incident = collaboration_optimizer.enrich_incident_query(
                incident_query, agent_context, collaboration_policy
            )
            session.metadata["used_rule_ids"] = agent_context.get_used_rule_ids() if agent_context else []
        else:
            agent_context = None
            collaboration_policy = CollaborationPolicy(
                max_iterations=cfg.pipeline.max_evidence_iterations,
                confidence_threshold=cfg.pipeline.hypothesis_confidence_threshold,
                require_reflection=False,
                memory_mode="off",
                rationale="Collaboration optimization disabled by configuration.",
            )
            enriched_incident = incident_query
        historical = store.get_historical_context(incident_query) if store else {"rules": [], "faults": []}
        rules = [r.get("text", r.get("condition", "")) for r in historical.get("rules", [])]
        faults = historical.get("faults", [])
        log(f"  Found {len(rules)} similar rules, {len(faults)} similar faults")
        log(f"  Collaboration strategy: {collaboration_policy.strategy} — {collaboration_policy.rationale}")
        session.complete_phase(1)
        emit({
            "event": "phase_complete",
            "phase": 1,
            "name": "CONTEXT_RETRIEVAL",
            "notes": f"{len(rules)} rules, {len(faults)} faults; strategy={collaboration_policy.strategy}",
        })
        emit({"event": "collaboration_policy", "data": collaboration_policy.to_dict()})

        # ── Step 2: Hypothesis Generation ──
        session.add_phase(2, "HYPOTHESIS_GENERATION")
        emit({"event": "phase_start", "phase": 2, "name": "HYPOTHESIS_GENERATION"})
        log("🧠 Generating root cause hypotheses...")
        session.hypotheses = hypothesis_agent.generate(
            enriched_incident, historical_rules=rules, historical_faults=faults
        )
        for h in session.hypotheses:
            log(f"  [{h.id}] (conf={h.confidence:.2f}) {h.description[:100]}")
        session.complete_phase(2, notes=f"{len(session.hypotheses)} hypotheses generated")
        emit({"event": "phase_complete", "phase": 2, "name": "HYPOTHESIS_GENERATION", "notes": f"{len(session.hypotheses)} hypotheses generated"})
        emit({"event": "hypotheses", "items": [{"id": h.id, "confidence": h.confidence, "description": h.description[:150]} for h in session.hypotheses]})

        # ── Step 3: Iterative Evidence Loop ──
        max_iter = collaboration_policy.max_iterations
        confidence_threshold = collaboration_policy.confidence_threshold
        
        for iteration in range(max_iter):
            session.current_iteration = iteration + 1
            session.add_phase(3, f"INVESTIGATION_ITER_{iteration+1}")
            emit({"event": "iteration", "current": iteration + 1, "total": max_iter})
            emit({"event": "phase_start", "phase": 3, "name": f"INVESTIGATION_ITER_{iteration+1}"})
            log(f"\n🔍 Investigation iteration {iteration+1}/{max_iter}")

            selected_agents = _selected_evidence_agents(incident_query, cfg, collaboration_policy)
            timeout_s = getattr(cfg.pipeline, "evidence_agent_timeout_s", 45)

            # Generate a detailed investigation plan only on the full path.
            # Fast evidence mode already selects a bounded evidence set, so an
            # extra planning LLM call just delays the first useful signal.
            if getattr(cfg.pipeline, "skip_planning_in_fast_evidence", True) and getattr(
                cfg.pipeline, "enable_fast_evidence_selection", True
            ):
                plan = {"plan": [{"agent": name, "reason": "fast evidence selection"} for name in selected_agents]}
                log(f"  📋 Fast evidence plan: {len(plan.get('plan', []))} agents")
            else:
                plan = planning_agent.generate_plan(
                    [h.to_dict() for h in session.hypotheses],
                    enriched_incident, iteration
                )
                log(f"  📋 Plan: {len(plan.get('plan', []))} steps")

            # Run domain agents in parallel
            log(f"  Running evidence agents: {', '.join(selected_agents)} (timeout={timeout_s}s)")
            start_time = time.time()

            def _agent_coro(name: str):
                if name == "metric_agent":
                    return metric_agent.analyze(_agent_query(
                        incident_query, agent_context, collaboration_policy, collaboration_optimizer, name
                    ), namespace)
                if name == "log_agent":
                    return log_agent.analyze(_agent_query(
                        incident_query, agent_context, collaboration_policy, collaboration_optimizer, name
                    ), namespace)
                if name == "trace_agent":
                    return trace_agent.analyze(_agent_query(
                        incident_query, agent_context, collaboration_policy, collaboration_optimizer, name
                    ), namespace=namespace)
                if name == "event_agent":
                    return event_agent.analyze(_agent_query(
                        incident_query, agent_context, collaboration_policy, collaboration_optimizer, name
                    ), namespace)
                if name == "llm_inference_agent":
                    return llm_inference_agent.analyze(_agent_query(
                        incident_query, agent_context, collaboration_policy, collaboration_optimizer, name
                    ), namespace)
                raise ValueError(f"Unknown evidence agent: {name}")

            results = await asyncio.gather(
                *[
                    _run_agent_with_timeout(name, _agent_coro(name), timeout_s)
                    for name in selected_agents
                ],
                return_exceptions=True,
            )

            # Collect results
            agent_names = selected_agents
            new_evidence = {}
            for name, result in zip(agent_names, results):
                if isinstance(result, Exception):
                    log(f"  ⚠️ {name} failed: {result}")
                    new_evidence[name] = {"summary": f"Error: {result}", "error": True}
                    emit({"event": "evidence", "agent": name, "summary": f"Error: {result}", "success": False})
                else:
                    log(f"  ✅ {name}: {result.get('summary', '')[:100]}")
                    new_evidence[name] = result
                    session.evidence[name] = result
                    emit({"event": "evidence", "agent": name, "summary": result.get("summary", "")[:200], "success": True})

            elapsed = time.time() - start_time
            log(f"  Evidence collection: {elapsed:.1f}s")
            if getattr(cfg.pipeline, "enable_structured_evidence", True):
                structured_items = _extract_evidence_items(session.evidence)
                if structured_items:
                    session.evidence["structured_evidence"] = {
                        "agent": "structured_evidence",
                        "items": structured_items,
                        "summary": f"{len(structured_items)} normalized evidence observations collected.",
                    }
                    emit({
                        "event": "structured_evidence",
                        "count": len(structured_items),
                        "items": structured_items[:20],
                    })

            # Re-rank hypotheses
            log("  Re-ranking hypotheses...")
            session.hypotheses = hypothesis_agent.rerank(session.hypotheses, new_evidence)
            top = session.top_hypothesis()
            if top:
                log(f"  Top hypothesis: [{top.id}] conf={top.confidence:.2f} — {top.description[:80]}")
            emit({"event": "hypotheses", "items": [{"id": h.id, "confidence": h.confidence, "description": h.description[:150]} for h in session.hypotheses]})

            session.iterations.append({
                "iteration": iteration + 1,
                "evidence_agents": list(new_evidence.keys()),
                "top_confidence": top.confidence if top else 0,
                "duration_s": round(elapsed, 1),
            })
            session.complete_phase(3, notes=f"Top confidence: {top.confidence:.2f}" if top else "")
            emit({"event": "phase_complete", "phase": 3, "name": f"INVESTIGATION_ITER_{iteration+1}", "notes": f"Top confidence: {top.confidence:.2f}" if top else ""})

            # Early exit if high confidence
            if top and top.confidence >= confidence_threshold:
                log(f"  🎯 High confidence reached ({top.confidence:.2f} ≥ {confidence_threshold}), stopping iterations")
                break

        # ── Step 4: Cross-Signal Correlation ──
        if cfg.pipeline.enable_correlation:
            session.add_phase(4, "CORRELATION")
            emit({"event": "phase_start", "phase": 4, "name": "CORRELATION"})
            log("\n🔗 Running cross-signal correlation...")
            try:
                correlation_result = correlation_agent.correlate(session.evidence)
                session.evidence["correlation"] = correlation_result
                log(f"  Top suspect: {correlation_result.get('top_suspect', 'N/A')}")
            except Exception as e:
                correlation_result = {}
                log(f"  ⚠️ Correlation failed: {e}")
            session.complete_phase(4)
            emit({"event": "phase_complete", "phase": 4, "name": "CORRELATION"})
        else:
            correlation_result = {}

        # ── Step 5: Graph RCA Localization ──
        if cfg.pipeline.enable_graph_rca:
            session.add_phase(5, "GRAPH_RCA")
            emit({"event": "phase_start", "phase": 5, "name": "GRAPH_RCA"})
            log("\n📊 Running graph-based RCA localization...")
            try:
                rca_tool = registry.get("rca_localization")
                if rca_tool and correlation_result:
                    ranked = correlation_result.get("anomaly_matrix", {}).get("ranked_services", [])
                    anomaly_scores = {s["service"]: s["composite_score"] for s in ranked}
                    if anomaly_scores:
                        rca_result = rca_tool.execute(anomaly_scores=anomaly_scores)
                        if rca_result.success:
                            session.evidence["graph_rca"] = rca_result.data
                            log(f"  Graph RCA top: {rca_result.data.get('top_root_cause', 'N/A')}")
            except Exception as e:
                log(f"  ⚠️ Graph RCA failed: {e}")
            session.complete_phase(5)
            emit({"event": "phase_complete", "phase": 5, "name": "GRAPH_RCA"})

        # ── Step 6: Final Report ──
        session.add_phase(6, "FINAL_REPORT")
        emit({"event": "phase_start", "phase": 6, "name": "FINAL_REPORT"})
        log("\n📝 Generating final RCA report...")

        hyp_text = "\n".join([
            f"[{h.id}] conf={h.confidence:.2f} — {h.description}"
            for h in session.hypotheses[:5]
        ])
        evidence_text = ""
        for agent, result in session.evidence.items():
            summary = result.get("summary", str(result))[:500]
            evidence_text += f"\n[{agent}]: {summary}\n"

        try:
            final_result = llm.json_chat([
                {"role": "system", "content": "You are an expert SRE producing the final RCA report."},
                {"role": "user", "content": FINAL_REPORT_PROMPT.format(
                    incident=incident_query,
                    hypotheses=hyp_text,
                    evidence=evidence_text[:6000],
                    correlation=str(correlation_result.get("summary", ""))[:2000],
                )}
            ])
        except Exception as e:
            log(f"  ⚠️ LLM report generation failed: {e}, using fallback")
            top = session.top_hypothesis()
            final_result = {
                "root_cause": top.description if top else f"Investigation of: {incident_query}",
                "confidence": top.confidence if top else 0.3,
                "fault_type": "unknown",
                "affected_services": [],
                "timeline": [],
                "evidence_summary": {k: str(v.get("summary", ""))[:200] for k, v in session.evidence.items() if isinstance(v, dict)},
                "reasoning_chain": f"LLM report generation failed ({e}), based on hypothesis analysis",
                "remediation_suggestion": "Investigate the reported incident manually",
                "prevention": "",
            }

        session.result = final_result
        log(f"\n🎯 Root Cause: {final_result.get('root_cause', 'N/A')}")
        log(f"   Confidence: {final_result.get('confidence', 0)}")
        session.complete_phase(6)
        emit({"event": "phase_complete", "phase": 6, "name": "FINAL_REPORT"})
        emit({"event": "result", "data": final_result})

        # ── Step 6.5: Collaboration Critic / Reflection ──
        collaboration_critique = None
        if collaboration_optimizer is not None and collaboration_policy.require_reflection:
            session.add_phase(10, "COLLABORATION_REFLECTION")
            emit({"event": "phase_start", "phase": 10, "name": "COLLABORATION_REFLECTION"})
            log("\n🧩 Running collaboration critic reflection...")
            try:
                collaboration_critique = collaboration_optimizer.critique_report(
                    incident_query=incident_query,
                    final_result=final_result,
                    evidence=session.evidence,
                    policy=collaboration_policy,
                )
                if collaboration_critique.get("needs_revision"):
                    final_result = collaboration_optimizer.improve_report(
                        incident_query=incident_query,
                        final_result=final_result,
                        evidence=session.evidence,
                        critique=collaboration_critique,
                        policy=collaboration_policy,
                    )
                    session.result = final_result
                    log(f"  Refined confidence: {final_result.get('confidence', 0)}")
                log(f"  Critic quality: {collaboration_critique.get('quality_score', 0):.3f}")
                emit({"event": "collaboration_critique", "data": collaboration_critique})
                emit({"event": "result", "data": final_result})
            except Exception as e:
                log(f"  ⚠️ Collaboration reflection failed: {e}")
                collaboration_critique = {"error": str(e), "needs_human_review": True}
            session.complete_phase(10)
            emit({"event": "phase_complete", "phase": 10, "name": "COLLABORATION_REFLECTION"})

        if collaboration_optimizer is not None:
            final_result = collaboration_optimizer.calibrate_fault_type(
                final_result=final_result,
                evidence=session.evidence,
                policy=collaboration_policy,
            )
            session.result = final_result
            if final_result.get("fault_type_original"):
                log(
                    "  Fault type calibrated: "
                    f"{final_result.get('fault_type_original')} → {final_result.get('fault_type')}"
                )
                emit({"event": "result", "data": final_result})

        # ── Step 7: Quality Judge ──
        session.add_phase(7, "QUALITY_JUDGE")
        emit({"event": "phase_start", "phase": 7, "name": "QUALITY_JUDGE"})
        log("\n⚖️ Running quality assessment...")
        try:
            reasoning = final_result.get("reasoning_chain", str(final_result))
            judge_result = judge.judge(
                reasoning=reasoning,
                root_cause=final_result.get("root_cause", ""),
                confidence=final_result.get("confidence", 0),
            )
            log(f"  Judge level: {judge_result['judge_level']}, score: {judge_result['combined_score']:.3f}")
            if judge_result["needs_review"]:
                log("  ⚠️ Flagged for review — low quality reasoning")
        except Exception as e:
            log(f"  ⚠️ Quality judge failed: {e}")
            judge_result = {"judge_level": "bronze", "combined_score": 0.0, "needs_review": True}
        session.complete_phase(7)
        emit({"event": "phase_complete", "phase": 7, "name": "QUALITY_JUDGE"})
        emit({"event": "judge", "data": judge_result})

        # ── Step 7.2: Diagnosis closure loop ──
        closure_result = None
        closure_plan = closure_manager.build_plan(
            rca_result=final_result,
            judge_result=judge_result,
            critique=collaboration_critique,
            evidence=session.evidence,
        )
        if closure_plan.should_iterate:
            session.add_phase(11, "DIAGNOSIS_CLOSURE")
            emit({"event": "phase_start", "phase": 11, "name": "DIAGNOSIS_CLOSURE"})
            emit({"event": "diagnosis_closure", "data": {"plan": closure_plan.to_dict(), "status": "started"}})
            log("\n🔁 Running diagnosis closure loop...")
            log(f"  Closure reason: {closure_plan.reason}")
            log(f"  Target agents: {', '.join(closure_plan.target_agents)}")

            closure_evidence = {}
            agent_map = {
                "metric_agent": metric_agent,
                "log_agent": log_agent,
                "trace_agent": trace_agent,
                "event_agent": event_agent,
                "llm_inference_agent": llm_inference_agent,
            }

            async def _run_closure_agent(agent_name: str):
                query = closure_plan.focus_queries.get(agent_name, incident_query)
                query = _agent_query(query, agent_context, collaboration_policy, collaboration_optimizer, agent_name)
                agent = agent_map[agent_name]
                if agent_name == "trace_agent":
                    return agent_name, await agent.analyze(query, namespace=namespace)
                if agent_name == "llm_inference_agent":
                    return agent_name, await agent.analyze(query, namespace=namespace)
                return agent_name, await agent.analyze(query, namespace)

            closure_tasks = [
                _run_closure_agent(agent_name)
                for agent_name in closure_plan.target_agents
                if agent_name in agent_map
            ]
            if closure_tasks:
                closure_results = await asyncio.gather(*closure_tasks, return_exceptions=True)
                for item in closure_results:
                    if isinstance(item, Exception):
                        closure_evidence[f"closure_error_{len(closure_evidence)+1}"] = {
                            "summary": f"Error: {item}",
                            "error": True,
                        }
                        continue
                    agent_name, result = item
                    key = f"{agent_name}_closure"
                    closure_evidence[key] = result
                    session.evidence[key] = result
                    emit({
                        "event": "evidence",
                        "agent": key,
                        "summary": result.get("summary", "")[:200] if isinstance(result, dict) else str(result)[:200],
                        "success": isinstance(result, dict) and not result.get("error"),
                    })

            if closure_evidence:
                revised = closure_manager.revise_report(
                    incident_query=incident_query,
                    original_result=final_result,
                    original_evidence=session.evidence,
                    closure_evidence=closure_evidence,
                    closure_plan=closure_plan,
                )
                if collaboration_optimizer is not None:
                    revised = collaboration_optimizer.calibrate_fault_type(
                        final_result=revised,
                        evidence=session.evidence,
                        policy=collaboration_policy,
                    )
                final_result = revised
                session.result = final_result
                reasoning = final_result.get("reasoning_chain", str(final_result))
                try:
                    judge_result = judge.judge(
                        reasoning=reasoning,
                        root_cause=final_result.get("root_cause", ""),
                        confidence=final_result.get("confidence", 0),
                    )
                except Exception as e:
                    log(f"  ⚠️ Closure re-judge failed: {e}")
                    judge_result = {**judge_result, "needs_review": True}

                closure_result = {
                    "plan": closure_plan.to_dict(),
                    "evidence_agents": list(closure_evidence.keys()),
                    "revised_result": final_result,
                    "judge_after": judge_result,
                }
                log(f"  Closure judge score: {judge_result.get('combined_score', 0):.3f}")
                emit({"event": "result", "data": final_result})
                emit({"event": "judge", "data": judge_result})
                emit({"event": "diagnosis_closure", "data": {"status": "completed", **closure_result}})
            else:
                closure_result = {"plan": closure_plan.to_dict(), "evidence_agents": [], "status": "no_evidence"}
                emit({"event": "diagnosis_closure", "data": closure_result})

            session.complete_phase(11)
            emit({"event": "phase_complete", "phase": 11, "name": "DIAGNOSIS_CLOSURE"})

        # ── Step 7.5: Human-in-the-loop quality gate ──
        review_result = None
        if judge_result.get("needs_review") or (collaboration_critique or {}).get("needs_human_review"):
            try:
                priority = "high" if judge_result.get("combined_score", 0) < 0.45 else "medium"
                reasons = ["RCA quality judge marked the result as needing human review."] if judge_result.get("needs_review") else []
                if (collaboration_critique or {}).get("needs_human_review"):
                    reasons.append("Collaboration critic requested human review for weak or conflicting evidence.")
                review_result = review_store.create_review(
                    incident_id=session_id,
                    reason=" ".join(reasons),
                    rca_result=final_result,
                    judge={**judge_result, "collaboration_critique": collaboration_critique, "closure": closure_result},
                    priority=priority,
                    source="rca_quality_gate",
                )
                log(f"  Human review queued: {review_result.get('review_id')}")
                emit({"event": "human_review", "data": review_result})
            except Exception as e:
                log(f"  ⚠️ Failed to queue human review: {e}")

        # ── Step 8: Auto-Learning ──
        if learner and cfg.memory.auto_learn:
            session.add_phase(8, "AUTO_LEARNING")
            emit({"event": "phase_start", "phase": 8, "name": "AUTO_LEARNING"})
            log("\n📖 Auto-learning from this incident...")
            learn_result = learner.learn_from_trace(
                reasoning_trace=reasoning,
                root_cause=final_result.get("root_cause", ""),
                confidence=final_result.get("confidence", 0),
                judge_level=judge_result["judge_level"],
            )
            learner.store_fault_context(
                incident_query, final_result, session.evidence,
                [h.to_dict() for h in session.hypotheses]
            )
            log(f"  Rules added: {learn_result.get('rules_added', 0)}")
            # Bump usage_count for rules that were surfaced into the prompt — feedback votes come later
            used_rule_ids = session.metadata.get("used_rule_ids", [])
            if used_rule_ids and store is not None:
                try:
                    bump = store.record_rule_usage(used_rule_ids, positive=None)
                    log(f"  Rule usage bumped: {bump.get('updated', 0)} of {len(used_rule_ids)}")
                except Exception as e:
                    log(f"  ⚠️ record_rule_usage failed: {e}")
            session.complete_phase(8)
            emit({"event": "phase_complete", "phase": 8, "name": "AUTO_LEARNING"})

        # ── Step 9: Optional Self-Healing ──
        if cfg.pipeline.enable_recovery and final_result.get("confidence", 0) >= cfg.remediation.confidence_threshold:
            session.add_phase(9, "RECOVERY")
            emit({"event": "phase_start", "phase": 9, "name": "RECOVERY"})
            log("\n🛠️ Initiating self-healing...")
            remediation_agent = RemediationAgent(llm, registry, cfg)
            rem_result = await remediation_agent.remediate(final_result, final_result.get("confidence", 0))
            session.evidence["remediation"] = rem_result
            log(f"  Remediation status: {rem_result.get('status', 'N/A')}")
            if rem_result.get("status") == "pending_approval":
                log(f"  📋 Remediation plan generated, waiting for approval")
                plan_actions = rem_result.get("plan", {}).get("actions", [])
                for a in plan_actions:
                    log(f"    [{a.get('risk_level','?')}] {a.get('description','')}")
            emit({"event": "remediation", "data": rem_result})
            session.complete_phase(9)
            emit({"event": "phase_complete", "phase": 9, "name": "RECOVERY"})

        # Surface used rule_ids before the evolution snapshot reads them — the snapshot
        # populates rules_recalled_count / rules_used_count from final_result.used_rule_ids.
        final_result["used_rule_ids"] = session.metadata.get("used_rule_ids", [])

        # ── Step 10: Evolution Snapshot ──
        evolution_snapshot = None
        evolution_report = None
        if evolution_tracker is not None and cfg.evolution.auto_record:
            try:
                evolution_payload = {
                    "result": final_result,
                    "judge": judge_result,
                    "metrics": {"latency_s": time.time() - pipe_trace.start_time if hasattr(pipe_trace, "start_time") else 0},
                    "collaboration": {
                        "policy": collaboration_policy.to_dict(),
                        "critique": collaboration_critique,
                        "closure": closure_result,
                    },
                }
                evolution_snapshot = evolution_tracker.record_snapshot(
                    fault_store=store,
                    feedback_store=feedback_store,
                    trace_store=trace_store,
                    rca_result=evolution_payload,
                    paradigm_name="rca_engine",
                    incident_query=incident_query,
                )
                evolution_report = evolution_tracker.get_evolution_report(fault_store=store)
                emit({"event": "evolution", "data": {"snapshot": evolution_snapshot, "report": evolution_report}})
            except Exception as e:
                log(f"  ⚠️ Evolution snapshot failed: {e}")

        # Complete — used_rule_ids was already attached to final_result above the snapshot
        # block so trace_store/expert_feedback can later close the recall→feedback loop.
        session.status = "completed"
        trace_store.complete_pipeline(session_id, final_result)
        
        return {
            "session_id": session_id,
            "status": "completed",
            "result": final_result,
            "judge": judge_result,
            "collaboration": {
                "policy": collaboration_policy.to_dict(),
                "critique": collaboration_critique,
                "closure": closure_result,
            },
            "human_review": review_result,
            "evolution": {
                "snapshot": evolution_snapshot,
                "report": evolution_report,
            },
            "hypotheses": [h.to_dict() for h in session.hypotheses],
            "phases": session.phases,
            "iterations": session.iterations,
            "evidence_agents": list(session.evidence.keys()),
        }

    except Exception as e:
        session.status = "failed"
        logger.error(f"RCA pipeline failed: {e}", exc_info=True)
        log(f"\n❌ Pipeline failed: {e}")
        return {
            "session_id": session_id,
            "status": "failed",
            "error": str(e),
            "phases": session.phases,
        }
