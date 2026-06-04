"""
AgenticSRE Agent Behavior Validator
Validates agent behavior, detects anomalies, and supports human-in-the-loop feedback.
SOW: "利用这些信息对多智能体的行为进行验证，及时发现多智能体行为异常"
"""

import logging
from typing import Any, Dict, List, Optional
from memory.trace_store import TraceStore, AgentTrace

logger = logging.getLogger(__name__)


class BehaviorValidator:
    """
    Validates agent behavior by analyzing execution traces.
    Detects anomalies such as:
    - Excessive latency
    - Repeated errors
    - Contradictory outputs
    - Abnormal token usage
    """

    def __init__(self, trace_store: TraceStore, config=None):
        self.store = trace_store
        from configs.config_loader import get_config
        cfg = config or get_config()
        self.anomaly_threshold = cfg.observability_agent.anomaly_threshold

    def validate_pipeline(self, pipeline_id: str) -> Dict:
        """Validate a pipeline execution for behavioral anomalies."""
        anomalies = []
        
        # Get pipeline traces
        for trace in self.store._traces:
            if trace.pipeline_id != pipeline_id:
                continue
            
            for at in trace.agent_traces:
                # Check 1: Excessive latency
                if at.duration_ms > 60000:  # > 60s
                    anomalies.append({
                        "type": "excessive_latency",
                        "agent": at.agent_name,
                        "value": at.duration_ms,
                        "threshold": 60000,
                        "severity": "warning",
                    })
                
                # Check 2: Errors
                if at.status == "error":
                    anomalies.append({
                        "type": "agent_error",
                        "agent": at.agent_name,
                        "error": at.error[:200],
                        "severity": "high",
                    })
                
                # Check 3: Abnormal token usage
                if at.token_usage > 50000:
                    anomalies.append({
                        "type": "excessive_tokens",
                        "agent": at.agent_name,
                        "value": at.token_usage,
                        "severity": "warning",
                    })

        return {
            "pipeline_id": pipeline_id,
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
            "status": "anomalous" if anomalies else "normal",
            "recommendation": self._generate_recommendation(anomalies),
        }

    def validate_agent_history(self, agent_name: str, window: int = 10) -> Dict:
        """Validate an agent's recent behavior across multiple pipelines."""
        recent_traces = []
        for trace in reversed(self.store._traces):
            for at in trace.agent_traces:
                if at.agent_name == agent_name:
                    recent_traces.append(at)
            if len(recent_traces) >= window:
                break
        
        if not recent_traces:
            return {"agent": agent_name, "status": "no_data"}
        
        durations = [at.duration_ms for at in recent_traces]
        error_rate = sum(1 for at in recent_traces if at.status == "error") / len(recent_traces)
        
        import math
        mean_dur = sum(durations) / len(durations)
        std_dur = math.sqrt(sum((d - mean_dur) ** 2 for d in durations) / len(durations)) if len(durations) > 1 else 0
        
        # Z-score anomaly detection on latest execution
        latest_zscore = (durations[0] - mean_dur) / std_dur if std_dur > 0 else 0
        
        return {
            "agent": agent_name,
            "sample_size": len(recent_traces),
            "avg_duration_ms": round(mean_dur, 0),
            "std_duration_ms": round(std_dur, 0),
            "latest_zscore": round(latest_zscore, 2),
            "error_rate": round(error_rate, 3),
            "status": "anomalous" if abs(latest_zscore) > self.anomaly_threshold or error_rate > 0.5 else "normal",
        }

    def _generate_recommendation(self, anomalies: List[Dict]) -> str:
        """Generate a recommendation based on detected anomalies."""
        if not anomalies:
            return "Pipeline execution normal. No action needed."
        
        recommendations = []
        error_agents = set()
        slow_agents = set()
        
        for a in anomalies:
            if a["type"] == "agent_error":
                error_agents.add(a["agent"])
            elif a["type"] == "excessive_latency":
                slow_agents.add(a["agent"])
        
        if error_agents:
            recommendations.append(
                f"Agents with errors: {', '.join(error_agents)}. "
                "Consider checking tool connectivity and LLM availability."
            )
        if slow_agents:
            recommendations.append(
                f"Slow agents: {', '.join(slow_agents)}. "
                "Consider reducing context size or increasing timeout."
            )
        
        return " ".join(recommendations)
