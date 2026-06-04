"""
AgenticSRE Agent Observability — Tracer
End-to-end execution tracing for all agent calls.
SOW: "设计多智能体可观测方案，实现对输入、输出、中间思考过程、性能指标与资源消耗的端到端全面观测"
"""

import time
import logging
import functools
from typing import Any, Callable, Dict, Optional

from memory.trace_store import TraceStore, AgentTrace

logger = logging.getLogger(__name__)


class AgentTracer:
    """
    Decorator/context manager for tracing agent executions.
    Records inputs, outputs, duration, token usage, and errors.
    """

    def __init__(self, trace_store: TraceStore, pipeline_id: str = "",
                 collect_tokens: bool = True, collect_latency: bool = True):
        self.store = trace_store
        self.pipeline_id = pipeline_id
        self.collect_tokens = collect_tokens
        self.collect_latency = collect_latency

    def trace(self, agent_name: str, action: str = "analyze"):
        """Decorator to trace an agent method call."""
        def decorator(func: Callable):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await self._trace_execution(func, agent_name, action, args, kwargs, is_async=True)
            
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                return self._trace_execution_sync(func, agent_name, action, args, kwargs)
            
            # Return appropriate wrapper based on function type
            import asyncio
            if asyncio.iscoroutinefunction(func):
                return async_wrapper
            return sync_wrapper
        return decorator

    async def _trace_execution(self, func, agent_name, action, args, kwargs, is_async=False):
        """Execute and trace a function call."""
        start = time.time()
        trace = AgentTrace(
            trace_id=f"{agent_name}-{int(start*1000)}",
            agent_name=agent_name,
            action=action,
            start_time=start,
            input_summary=self._summarize_input(kwargs),
        )

        try:
            result = await func(*args, **kwargs)
            trace.status = "success"
            trace.output_summary = self._summarize_output(result)
            return result
        except Exception as e:
            trace.status = "error"
            trace.error = str(e)
            raise
        finally:
            trace.end_time = time.time()
            trace.duration_ms = (trace.end_time - trace.start_time) * 1000
            if self.pipeline_id:
                self.store.add_agent_trace(self.pipeline_id, trace)
            logger.debug(
                f"[TRACE] {agent_name}.{action} — "
                f"{trace.duration_ms:.0f}ms — {trace.status}"
            )

    def _trace_execution_sync(self, func, agent_name, action, args, kwargs):
        """Synchronous trace execution."""
        start = time.time()
        trace = AgentTrace(
            trace_id=f"{agent_name}-{int(start*1000)}",
            agent_name=agent_name,
            action=action,
            start_time=start,
            input_summary=self._summarize_input(kwargs),
        )

        try:
            result = func(*args, **kwargs)
            trace.status = "success"
            trace.output_summary = self._summarize_output(result)
            return result
        except Exception as e:
            trace.status = "error"
            trace.error = str(e)
            raise
        finally:
            trace.end_time = time.time()
            trace.duration_ms = (trace.end_time - trace.start_time) * 1000
            if self.pipeline_id:
                self.store.add_agent_trace(self.pipeline_id, trace)

    def _summarize_input(self, kwargs: Dict) -> str:
        """Summarize input for trace record."""
        parts = []
        for k, v in kwargs.items():
            sv = str(v)[:100]
            parts.append(f"{k}={sv}")
        return "; ".join(parts)[:500]

    def _summarize_output(self, result: Any) -> str:
        """Summarize output for trace record."""
        if isinstance(result, dict):
            keys = list(result.keys())
            return f"Dict with keys: {keys}"
        return str(result)[:500]


class MetricsCollector:
    """
    Collects performance metrics across pipeline executions.
    Tracks latency, token usage, error rates, and resource consumption.
    """

    def __init__(self):
        self._metrics: Dict[str, list] = {
            "latency_ms": [],
            "token_usage": [],
            "error_count": 0,
            "success_count": 0,
            "agent_calls": {},
        }

    def record(self, agent_name: str, duration_ms: float, tokens: int = 0,
               success: bool = True):
        """Record a metric observation."""
        self._metrics["latency_ms"].append(duration_ms)
        self._metrics["token_usage"].append(tokens)
        if success:
            self._metrics["success_count"] += 1
        else:
            self._metrics["error_count"] += 1
        
        if agent_name not in self._metrics["agent_calls"]:
            self._metrics["agent_calls"][agent_name] = {"count": 0, "errors": 0, "total_ms": 0}
        self._metrics["agent_calls"][agent_name]["count"] += 1
        self._metrics["agent_calls"][agent_name]["total_ms"] += duration_ms
        if not success:
            self._metrics["agent_calls"][agent_name]["errors"] += 1

    def summary(self) -> Dict:
        """Get metrics summary."""
        latencies = self._metrics["latency_ms"]
        tokens = self._metrics["token_usage"]
        
        return {
            "total_calls": self._metrics["success_count"] + self._metrics["error_count"],
            "success_rate": self._metrics["success_count"] / max(
                self._metrics["success_count"] + self._metrics["error_count"], 1
            ),
            "avg_latency_ms": sum(latencies) / max(len(latencies), 1),
            "p95_latency_ms": sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0,
            "total_tokens": sum(tokens),
            "per_agent": self._metrics["agent_calls"],
        }

    def reset(self):
        """Reset all metrics."""
        self.__init__()
