"""
AgenticSRE Paradigms Package
Multi-agent collaboration paradigm comparison framework.

Supports 6 paradigms:
- chain: Sequential chain (Event→Metric→Log→Trace)
- react: ReAct loop (Thought→Action→Observation)
- reflection: Self-reflection (analyze→critique→re-investigate→improve)
- plan_and_execute: Hypothesis-driven (hypothesize→plan→investigate→re-rank)
- debate: Multi-perspective (infra/app/holistic→moderator)
- voting: Ensemble voting (3 LLM analyses→majority vote)
"""

# Import registry API
from paradigms.registry import (
    get_paradigm,
    list_paradigms,
    paradigm_names,
    register_paradigm,
)

# Import base types
from paradigms.base import (
    AgentPool,
    ParadigmBase,
    ParadigmMetrics,
    ParadigmResult,
)

# Import all paradigm modules to trigger @register_paradigm registration
import paradigms.chain            # noqa: F401
import paradigms.react            # noqa: F401
import paradigms.reflection       # noqa: F401
import paradigms.plan_and_execute # noqa: F401
import paradigms.debate           # noqa: F401
import paradigms.voting           # noqa: F401

__all__ = [
    # Registry
    "get_paradigm",
    "list_paradigms",
    "paradigm_names",
    "register_paradigm",
    # Base types
    "AgentPool",
    "ParadigmBase",
    "ParadigmMetrics",
    "ParadigmResult",
]
