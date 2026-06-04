"""
AgenticSRE Remediation Agent
Self-healing agent with plan→execute→verify→rollback cycle.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient
from tools.action_stack import ActionStack, Action

logger = logging.getLogger(__name__)


class RemediationAgent:
    """
    Self-healing remediation agent.
    
    Flow: LLM generates NL plan → converts to kubectl commands → 
    executes with ActionStack rollback → verifies cluster health.
    """

    PLAN_PROMPT = """You are an expert SRE responsible for remediating Kubernetes issues.
Given the root cause analysis result, generate a safe remediation plan.

RCA Result:
{rca_result}

Cluster Context:
{cluster_context}

Safety constraints:
- NEVER delete namespaces, nodes, or persistent volumes
- ALWAYS prefer scaling over deletion
- Use rolling restart over hard restart
- Ensure rollback commands exist for every action

Generate a remediation plan:
{{
    "actions": [
        {{
            "description": "what this action does",
            "command": "kubectl command to execute",
            "rollback_command": "kubectl command to undo this",
            "risk_level": "low|medium|high",
            "verification": "how to verify the action succeeded"
        }}
    ],
    "estimated_recovery_time": "2m",
    "requires_approval": true
}}"""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, config=None):
        self.llm = llm
        self.registry = registry
        self.action_stack = ActionStack(max_depth=10)
        
        from configs.config_loader import get_config
        cfg = config or get_config()
        self.confidence_threshold = cfg.remediation.confidence_threshold
        self.require_approval = cfg.remediation.require_approval
        self.enabled = cfg.remediation.enabled

    async def remediate(self, rca_result: Dict, confidence: float = 0.0,
                         approved: bool = False) -> Dict:
        """Execute remediation based on RCA results."""
        
        if not self.enabled:
            return {"status": "disabled", "message": "Self-healing is disabled in configuration"}

        if confidence < self.confidence_threshold:
            return {
                "status": "skipped",
                "message": f"Confidence {confidence:.2f} below threshold {self.confidence_threshold}",
            }

        if self.require_approval and not approved:
            plan = self._generate_plan(rca_result)
            return {
                "status": "pending_approval",
                "plan": plan,
                "message": "Remediation plan requires approval before execution",
            }

        # Generate and execute plan
        plan = self._generate_plan(rca_result)
        results = []
        
        for action_spec in plan.get("actions", []):
            # Create action with rollback
            action = Action(
                action_id=f"rem-{uuid.uuid4().hex[:8]}",
                description=action_spec.get("description", ""),
                command=action_spec.get("command", ""),
                rollback_command=action_spec.get("rollback_command", ""),
            )
            
            # Execute
            result = self._execute_command(action.command)
            action.status = "executed" if result.success else "failed"
            action.result = str(result.data)[:500] if result.success else result.error
            
            # Push to stack for rollback
            self.action_stack.push(action)
            
            results.append({
                "action_id": action.action_id,
                "description": action.description,
                "status": action.status,
                "result": action.result,
            })
            
            if not result.success:
                logger.error(f"Remediation action failed: {action.description}")
                break

        # Verify cluster health after remediation
        verification = self._verify_health()

        return {
            "status": "executed",
            "actions": results,
            "verification": verification,
            "rollback_available": len(self.action_stack) > 0,
        }

    def rollback(self) -> Dict:
        """Roll back all remediation actions."""
        def executor(cmd: str) -> str:
            result = self._execute_command(cmd)
            return result.data if result.success else result.error
        
        results = self.action_stack.rollback_all(executor)
        return {"status": "rolled_back", "actions": results}

    def _generate_plan(self, rca_result: Dict) -> Dict:
        """Generate remediation plan via LLM."""
        # Get cluster context
        cluster_context = ""
        k8s_health = self.registry.get("k8s_health")
        if k8s_health:
            result = k8s_health.execute(component="pods")
            if result.success:
                cluster_context = str(result.data)[:2000]

        try:
            return self.llm.json_chat([
                {"role": "system", "content": "You are an expert SRE remediation planner."},
                {"role": "user", "content": self.PLAN_PROMPT.format(
                    rca_result=str(rca_result)[:3000],
                    cluster_context=cluster_context,
                )}
            ])
        except Exception as e:
            logger.error(f"Plan generation failed: {e}")
            return {"actions": [], "error": str(e)}

    def _execute_command(self, command: str) -> ToolResult:
        """Execute a kubectl command."""
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return ToolResult(success=False, error="kubectl tool not available")
        # Strip 'kubectl' prefix if present
        cmd = command.strip()
        if cmd.startswith("kubectl "):
            cmd = cmd[8:]
        return kubectl.execute(command=cmd)

    def _verify_health(self) -> Dict:
        """Verify cluster health after remediation."""
        k8s_health = self.registry.get("k8s_health")
        if k8s_health is None:
            return {"status": "unknown", "note": "health check tool not available"}
        result = k8s_health.execute(component="all")
        return result.data if result.success else {"status": "error", "error": result.error}
