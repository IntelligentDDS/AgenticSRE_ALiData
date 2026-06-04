"""
AgenticSRE RCA Localization Tool
Graph-based PageRank-style anomaly propagation for root cause localization.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

from tools.base_tool import SRETool, ToolResult

logger = logging.getLogger(__name__)


class RCALocalizationTool(SRETool):
    """Graph-based root cause localization using PageRank-style score propagation."""

    name = "rca_localization"
    description = "Perform graph-based RCA using service dependency graph and anomaly scores"

    def _execute(
        self,
        anomaly_scores: Dict[str, float] = None,
        dependency_edges: List[Dict] = None,
        iterations: int = 20,
        damping: float = 0.85,
    ) -> ToolResult:
        if not anomaly_scores:
            return ToolResult(success=False, error="No anomaly scores provided")

        # Build adjacency graph
        graph = defaultdict(list)      # node -> [downstream nodes]
        in_degree = defaultdict(int)
        all_nodes = set(anomaly_scores.keys())

        if dependency_edges:
            for edge in dependency_edges:
                src = edge.get("source", edge.get("from", ""))
                dst = edge.get("target", edge.get("to", ""))
                if src and dst:
                    graph[src].append(dst)
                    in_degree[dst] += 1
                    all_nodes.add(src)
                    all_nodes.add(dst)

        # Initialize scores
        n = len(all_nodes)
        if n == 0:
            return ToolResult(success=False, error="No nodes in graph")

        scores = {}
        for node in all_nodes:
            # Initial score = anomaly score (normalized) + base
            base_anomaly = anomaly_scores.get(node, 0.0)
            scores[node] = base_anomaly / max(max(anomaly_scores.values()), 1e-10)

        # PageRank-style propagation
        for _ in range(iterations):
            new_scores = {}
            for node in all_nodes:
                # Score from incoming edges (upstream propagation)
                incoming_score = 0.0
                for src, dsts in graph.items():
                    if node in dsts:
                        out_degree = len(dsts)
                        incoming_score += scores.get(src, 0) / max(out_degree, 1)
                
                # Combine anomaly score + propagated score
                anomaly_base = anomaly_scores.get(node, 0.0) / max(max(anomaly_scores.values()), 1e-10)
                new_scores[node] = (1 - damping) * anomaly_base + damping * incoming_score
            
            scores = new_scores

        # Normalize final scores
        max_score = max(scores.values()) if scores else 1
        if max_score > 0:
            scores = {k: round(v / max_score, 4) for k, v in scores.items()}

        # Rank
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        return ToolResult(success=True, data={
            "ranked_root_causes": [
                {
                    "service": node,
                    "rca_score": score,
                    "anomaly_score": round(anomaly_scores.get(node, 0), 4),
                    "in_degree": in_degree.get(node, 0),
                    "out_degree": len(graph.get(node, [])),
                }
                for node, score in ranked
            ],
            "top_root_cause": ranked[0][0] if ranked else None,
            "confidence": ranked[0][1] if ranked else 0,
        })

    def _parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "anomaly_scores": {
                    "type": "object",
                    "description": "Map of service name to anomaly score",
                    "additionalProperties": {"type": "number"}
                },
                "dependency_edges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "target": {"type": "string"},
                        }
                    },
                    "description": "Service dependency edges"
                },
                "iterations": {"type": "integer", "default": 20},
                "damping": {"type": "number", "default": 0.85},
            },
            "required": ["anomaly_scores"]
        }
