"""All-agents smoke harness — exercise every agent's primary method through the MCP backend."""
import asyncio
import sys
import time
import traceback

sys.path.insert(0, "/root/cpf/AgenticSRE_MCP")

from tools import build_tool_registry
from agents.detection_agent import DetectionAgent, DetectionSignal


class StubLLM:
    """Stand-in LLM. Returns canned strings for both sync and async APIs."""
    SENTINEL = "Stub LLM response (not a real inference)."

    def __init__(self):
        self.model = "stub"

    def complete(self, *args, **kwargs): return self.SENTINEL
    def chat(self, *args, **kwargs): return self.SENTINEL

    async def complete_async(self, *args, **kwargs): return self.SENTINEL
    async def chat_async(self, *args, **kwargs): return self.SENTINEL
    async def async_chat(self, *args, **kwargs): return self.SENTINEL
    async def async_complete(self, *args, **kwargs): return self.SENTINEL

    def __getattr__(self, name):
        # Any unknown attribute → return a method that's safe in both
        # sync and async call sites. If the caller does ``await stub.foo(...)``
        # it gets the SENTINEL via the awaitable wrapper; if it calls
        # ``stub.foo(...)`` it gets the SENTINEL directly.
        sentinel = self.SENTINEL
        if name.startswith("async_") or name.endswith("_async") or name in {"acomplete", "achat"}:
            async def _async_stub(*a, **kw): return sentinel
            return _async_stub
        def _sync_stub(*a, **kw): return sentinel
        return _sync_stub


results = []

def run_agent(label, fn, *args, **kwargs):
    t0 = time.time()
    try:
        out = fn(*args, **kwargs)
        if asyncio.iscoroutine(out):
            out = asyncio.run(out)
        dt = time.time() - t0
        kind = type(out).__name__
        size = len(out) if hasattr(out, "__len__") else "-"
        results.append((label, "PASS", dt, f"{kind} size={size}"))
    except Exception as e:
        dt = time.time() - t0
        results.append((label, "FAIL", dt, repr(e)[:160]))
        traceback.print_exc()


reg = build_tool_registry()
llm = StubLLM()

# 1) DetectionAgent
from agents.detection_agent import DetectionAgent
agent_det = DetectionAgent(llm=llm, registry=reg)
run_agent("DetectionAgent.detect", agent_det.detect, "cms-demo")

# Build a fake detection signal for downstream agents
signal = DetectionSignal(
    signal_id="sig-test-001",
    source="prometheus",
    severity="warning",
    title="High CPU on recommendation pod",
    description="cpu_usage_rate > 0.8 for 5min",
    namespace="cms-demo",
    service="recommendation",
)

# 2) AlertAgent
from agents.alert_agent import AlertAgent
agent_alert = AlertAgent(llm=llm, registry=reg)
run_agent("AlertAgent.compress_and_recommend", agent_alert.compress_and_recommend, None, "cms-demo")

# 3) MetricAgent
from agents.metric_agent import MetricAgent
agent_metric = MetricAgent(llm=llm, registry=reg)
run_agent("MetricAgent.analyze", agent_metric.analyze, "high cpu", "cms-demo")

# 4) LogAgent
from agents.log_agent import LogAgent
agent_log = LogAgent(llm=llm, registry=reg)
run_agent("LogAgent.analyze", agent_log.analyze, "error", "cms-demo")

# 5) TraceAgent
from agents.trace_agent import TraceAgent
agent_trace = TraceAgent(llm=llm, registry=reg)
run_agent("TraceAgent.analyze", agent_trace.analyze, "slow request", "recommendation")

# 6) EventAgent
from agents.event_agent import EventAgent
agent_event = EventAgent(llm=llm, registry=reg)
run_agent("EventAgent.analyze", agent_event.analyze, "crashloop", "cms-demo")

# 7) ProfilingAgent
from agents.profiling_agent import ProfilingAgent
agent_prof = ProfilingAgent(llm=llm, registry=reg)
run_agent("ProfilingAgent.analyze", agent_prof.analyze, "cpu profile", "cms-demo")

# 8) LLMInferenceAgent
from agents.llm_inference_agent import LLMInferenceAgent
agent_llmi = LLMInferenceAgent(llm=llm, registry=reg)
run_agent("LLMInferenceAgent.analyze", agent_llmi.analyze, "vllm latency", "cms-demo")

# 9) HypothesisAgent
from agents.hypothesis_agent import HypothesisAgent
agent_hyp = HypothesisAgent(llm=llm)
run_agent("HypothesisAgent.generate", agent_hyp.generate, signal.title)

# 10) PlanningAgent
from agents.planning_agent import PlanningAgent
agent_plan = PlanningAgent(llm=llm, registry=reg)
run_agent("PlanningAgent.generate_plan", agent_plan.generate_plan, [], signal.title, 1)

# 11) CorrelationAgent
from agents.correlation_agent import CorrelationAgent
agent_corr = CorrelationAgent(llm=llm)
run_agent("CorrelationAgent.correlate", agent_corr.correlate, {"metric": {}, "log": {}, "trace": {}})

# 12) RemediationAgent
from agents.remediation_agent import RemediationAgent
agent_rem = RemediationAgent(llm=llm, registry=reg)
run_agent("RemediationAgent.remediate", agent_rem.remediate, {"root_cause": "high_cpu"}, 0.9, signal.namespace)

# 13) MetricAnomalyDetector
from agents.metric_anomaly_detector import MetricAnomalyDetector
prom_tool = reg.get("prometheus")
mad = MetricAnomalyDetector(
    prom_tool=prom_tool,
    metric_checks=[],
    detection_cfg={
        "default_detect_methods": ["threshold"],
        "default_lookback_m": 5,
        "default_z_threshold": 3.0,
        "default_ewma_span": 10,
        "categories_enabled": {},
        "business_services": [],
        "db_services": [],
        "thresholds": {},
        "namespace": "cms-demo",
    },
)
run_agent("MetricAnomalyDetector.detect", mad.detect, "cms-demo")

print()
print("=" * 80)
print(f"{'Agent':<45} {'Result':<6} {'Time':>8}  Detail")
print("-" * 80)
for label, status, dt, detail in results:
    print(f"{label:<45} {status:<6} {dt*1000:>6.0f}ms  {detail}")
print("=" * 80)
n_pass = sum(1 for r in results if r[1] == "PASS")
n_fail = sum(1 for r in results if r[1] == "FAIL")
print(f"Summary: {n_pass} PASS, {n_fail} FAIL out of {len(results)} agents")
sys.exit(0 if n_fail == 0 else 1)
