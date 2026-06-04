"""
AgenticSRE Event Agent
Fetches and analyzes Kubernetes events and resource descriptions.
"""

import logging
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


class EventAgent:
    """
    K8s event analysis agent: fetches Warning events, resource status,
    and pod conditions to identify cluster-level issues.
    """

    SYSTEM_PROMPT = """You are a Kubernetes SRE event analysis expert.
Analyze the provided Kubernetes events and resource states.
Identify:
1. Warning/Error events and their targets (pods, nodes, deployments)
2. Event patterns (repeated events indicating persistent issues)
3. Resource status anomalies (NotReady nodes, CrashLoopBackOff pods)
4. Recent scheduling/scaling/health-check failures
5. Timeline of events related to the incident

Be specific about event types, involved objects, and counts. Format as structured analysis."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
        self.summary_max_tokens = 1024

    async def analyze(self, query: str, namespace: str = "") -> Dict:
        """Run K8s event analysis for the given incident query."""
        
        # Fetch events
        events = self._fetch_events(namespace)
        
        # Fetch node status
        nodes = self._fetch_node_status()
        
        # Fetch problem pods
        pods = self._fetch_problem_pods(namespace)
        
        # Fetch resource descriptions for problem resources
        descriptions = {}
        if pods:
            for pod in pods[:5]:
                desc = self._describe_resource("pod", pod.get("name", ""), pod.get("namespace", namespace))
                if desc:
                    descriptions[pod["name"]] = desc[:1000]

        # LLM summarization
        context = f"Incident: {query}\n\n"
        context += f"K8s Events:\n{events[:3000] if events else 'No events found'}\n\n"
        context += f"Node Status:\n{nodes[:2000] if nodes else 'No node data'}\n\n"
        context += f"Problem Pods ({len(pods) if pods else 0}):\n"
        if pods:
            for pod in pods[:10]:
                context += f"  - {pod}\n"
        if descriptions:
            context += f"\nResource Descriptions:\n"
            for name, desc in descriptions.items():
                context += f"\n[{name}]:\n{desc}\n"

        summary = await self.llm.async_chat([
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": context[:8000]}
        ], max_tokens=self.summary_max_tokens)

        return {
            "agent": "event_agent",
            "query": query,
            "warning_events": events,
            "problem_pods": pods,
            "node_status": nodes,
            "summary": summary,
        }

    def _fetch_events(self, namespace: str = "") -> Optional[str]:
        """Fetch K8s warning events."""
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return None
        cmd = "get events --sort-by='.lastTimestamp' --field-selector type=Warning"
        if namespace:
            cmd += f" -n {namespace}"
        else:
            cmd += " --all-namespaces"
        result = kubectl.execute(command=cmd)
        return result.data if result.success else None

    def _fetch_node_status(self) -> Optional[str]:
        """Fetch node status."""
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return None
        result = kubectl.execute(command="get nodes -o wide")
        return result.data if result.success else None

    def _fetch_problem_pods(self, namespace: str = "") -> List[Dict]:
        """Find pods in problematic states."""
        k8s_res = self.registry.get("k8s_resources")
        if k8s_res is None:
            return []
        result = k8s_res.execute(
            action="list", resource_type="pods",
            namespace=namespace or "", output="json"
        )
        if not result.success:
            return []
        
        import json
        try:
            data = json.loads(result.data) if isinstance(result.data, str) else result.data
        except (json.JSONDecodeError, TypeError):
            return []
        
        problems = []
        for pod in data.get("items", []):
            phase = pod.get("status", {}).get("phase", "")
            name = pod["metadata"]["name"]
            ns = pod["metadata"]["namespace"]
            
            for cs in pod.get("status", {}).get("containerStatuses", []):
                waiting = cs.get("state", {}).get("waiting", {})
                reason = waiting.get("reason", "")
                if reason in ("CrashLoopBackOff", "ImagePullBackOff", "OOMKilled", "Error", "CreateContainerError"):
                    problems.append({
                        "name": name, "namespace": ns,
                        "reason": reason,
                        "restart_count": cs.get("restartCount", 0),
                        "message": waiting.get("message", "")[:200],
                    })
            
            if phase not in ("Running", "Succeeded") and not any(p["name"] == name for p in problems):
                problems.append({"name": name, "namespace": ns, "phase": phase})
        
        return problems

    def _describe_resource(self, kind: str, name: str, namespace: str) -> Optional[str]:
        """Get kubectl describe output for a resource."""
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return None
        result = kubectl.execute(command=f"describe {kind} {name}", namespace=namespace)
        return result.data if result.success else None
