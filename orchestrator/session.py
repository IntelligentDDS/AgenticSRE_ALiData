"""
AgenticSRE Session State
Holds per-session mutable state for RCA pipeline execution.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents.hypothesis_agent import Hypothesis


@dataclass
class RCASession:
    """Per-session mutable state container for the RCA pipeline."""
    
    session_id: str = ""
    incident_query: str = ""
    namespace: str = ""
    
    # Hypotheses
    hypotheses: List[Hypothesis] = field(default_factory=list)
    
    # Evidence store (agent_name -> result dict)
    evidence: Dict[str, Dict] = field(default_factory=dict)
    
    # Iteration history
    iterations: List[Dict] = field(default_factory=list)
    current_iteration: int = 0
    
    # Phases
    phases: List[Dict] = field(default_factory=list)
    
    # Final result
    result: Optional[Dict] = None
    
    # Status
    status: str = "initialized"  # initialized | running | completed | failed
    
    # Log for streaming
    log_lines: List[str] = field(default_factory=list)

    # Evolution-loop bookkeeping (rule_ids surfaced from ContextBuilder, etc.)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def log(self, message: str):
        """Add a log line."""
        import time
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        self.log_lines.append(line)

    def add_phase(self, phase_num: int, name: str, status: str = "running",
                  notes: str = "") -> Dict:
        """Record a pipeline phase."""
        import time
        phase = {
            "phase": phase_num,
            "name": name,
            "status": status,
            "start_time": time.time(),
            "end_time": 0,
            "notes": notes,
        }
        self.phases.append(phase)
        return phase

    def complete_phase(self, phase_num: int, status: str = "completed", notes: str = ""):
        """Mark a phase as complete."""
        import time
        for p in reversed(self.phases):
            if p["phase"] == phase_num:
                p["status"] = status
                p["end_time"] = time.time()
                if notes:
                    p["notes"] = notes
                break

    def top_hypothesis(self) -> Optional[Hypothesis]:
        """Get the highest-confidence hypothesis."""
        if not self.hypotheses:
            return None
        return max(self.hypotheses, key=lambda h: h.confidence)

    def to_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "incident_query": self.incident_query,
            "status": self.status,
            "current_iteration": self.current_iteration,
            "hypothesis_count": len(self.hypotheses),
            "top_hypothesis": self.top_hypothesis().to_dict() if self.top_hypothesis() else None,
            "evidence_agents": list(self.evidence.keys()),
            "phases": self.phases,
            "result": self.result,
        }
