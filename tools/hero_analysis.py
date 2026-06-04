"""
AgenticSRE Hero Analysis Engine
Ported from Hero project: log pattern analysis, metric 3σ detection,
trace latency analysis, plus WeRCA Drain3 log clustering and Pearson onset detection.
"""

import math
import logging
import hashlib
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter, defaultdict

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
#  Metric Analysis (Hero 3σ + WeRCA Onset)
# ─────────────────────────────────────────

class HeroMetricAnalyzer:
    """Hero-style metric anomaly detection with 3σ rule + WeRCA Pearson onset."""

    @staticmethod
    def three_sigma_detect(values: List[float], timestamps: Optional[List[float]] = None) -> Dict:
        """Detect anomalies using 3-sigma rule."""
        if len(values) < 5:
            return {"anomalies": [], "note": "insufficient data"}
        
        n = len(values)
        mean = sum(values) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
        
        if std == 0:
            return {"mean": mean, "std": 0, "anomalies": []}
        
        anomalies = []
        for i, v in enumerate(values):
            z = (v - mean) / std
            if abs(z) > 3:
                anomalies.append({
                    "index": i,
                    "timestamp": timestamps[i] if timestamps else i,
                    "value": v,
                    "zscore": round(z, 2),
                    "severity": "critical" if abs(z) > 4.5 else "high" if abs(z) > 3.5 else "medium",
                })
        
        return {
            "mean": round(mean, 4),
            "std": round(std, 4),
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
        }

    @staticmethod
    def pearson_onset_detection(
        values: List[float],
        window_size: int = 10,
        threshold: float = 0.7,
    ) -> Dict:
        """WeRCA-style Pearson onset detection for finding anomaly start points."""
        if len(values) < window_size * 2:
            return {"onset_points": [], "note": "insufficient data"}
        
        onset_points = []
        for i in range(window_size, len(values) - window_size):
            before = values[i - window_size:i]
            after = values[i:i + window_size]
            
            # Pearson correlation between before and after windows
            corr = HeroMetricAnalyzer._pearson(before, after)
            
            if corr is not None and corr < threshold:
                # Mean shift detection
                mean_before = sum(before) / len(before)
                mean_after = sum(after) / len(after)
                shift = abs(mean_after - mean_before) / (abs(mean_before) + 1e-10)
                
                if shift > 0.3:  # Significant mean shift
                    onset_points.append({
                        "index": i,
                        "pearson_corr": round(corr, 3),
                        "mean_shift": round(shift, 3),
                        "direction": "increase" if mean_after > mean_before else "decrease",
                    })
        
        # Deduplicate nearby onset points (keep strongest)
        filtered = []
        for p in onset_points:
            if not filtered or p["index"] - filtered[-1]["index"] > window_size:
                filtered.append(p)
            elif abs(p["pearson_corr"]) < abs(filtered[-1]["pearson_corr"]):
                filtered[-1] = p
        
        return {"onset_points": filtered}

    @staticmethod
    def _pearson(x: List[float], y: List[float]) -> Optional[float]:
        n = min(len(x), len(y))
        if n < 3:
            return None
        mx = sum(x[:n]) / n
        my = sum(y[:n]) / n
        cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
        sx = math.sqrt(sum((x[i] - mx) ** 2 for i in range(n)))
        sy = math.sqrt(sum((y[i] - my) ** 2 for i in range(n)))
        if sx * sy == 0:
            return None
        return cov / (sx * sy)


# ─────────────────────────────────────────
#  Log Analysis (Hero Pattern + WeRCA Drain3)
# ─────────────────────────────────────────

class HeroLogAnalyzer:
    """Hero-style log pattern analysis with optional Drain3 clustering."""

    @staticmethod
    def pattern_analysis(log_entries: List[str], top_k: int = 20) -> Dict:
        """Extract common patterns and anomalous log lines."""
        if not log_entries:
            return {"patterns": [], "anomalies": []}
        
        # Simple pattern extraction: normalize numbers and hashes
        import re
        patterns = Counter()
        entry_to_pattern = {}
        
        for entry in log_entries:
            # Normalize: replace numbers, IPs, UUIDs, hashes
            normalized = re.sub(r'\d+\.\d+\.\d+\.\d+', '<IP>', entry)
            normalized = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<UUID>', normalized)
            normalized = re.sub(r'\b\d+\b', '<NUM>', normalized)
            normalized = re.sub(r'\b[0-9a-f]{32,}\b', '<HASH>', normalized)
            patterns[normalized] += 1
            entry_to_pattern[entry] = normalized
        
        # Sort patterns by frequency
        common_patterns = patterns.most_common(top_k)
        
        # Find rare patterns (potential anomalies)
        rare_threshold = max(1, len(log_entries) * 0.01)
        rare_patterns = [
            {"pattern": p, "count": c, "sample": next(
                (e for e, pat in entry_to_pattern.items() if pat == p), ""
            )[:200]}
            for p, c in patterns.items()
            if c <= rare_threshold
        ]
        
        # Error/warning detection
        error_entries = [e for e in log_entries if any(
            kw in e.lower() for kw in ("error", "fail", "panic", "fatal", "exception", "crash")
        )]
        
        return {
            "total_entries": len(log_entries),
            "unique_patterns": len(patterns),
            "top_patterns": [
                {"pattern": p[:200], "count": c} for p, c in common_patterns
            ],
            "rare_patterns_count": len(rare_patterns),
            "rare_patterns": rare_patterns[:10],
            "error_count": len(error_entries),
            "error_samples": [e[:200] for e in error_entries[:5]],
        }

    @staticmethod
    def drain3_cluster(log_entries: List[str]) -> Dict:
        """Cluster log entries using Drain3 algorithm."""
        try:
            from drain3 import TemplateMiner
            from drain3.template_miner_config import TemplateMinerConfig
            
            config = TemplateMinerConfig()
            config.drain_sim_th = 0.4
            config.drain_depth = 4
            miner = TemplateMiner(config=config)
            
            for entry in log_entries:
                miner.add_log_message(entry)
            
            clusters = []
            for cluster in miner.drain.clusters:
                clusters.append({
                    "cluster_id": cluster.cluster_id,
                    "template": str(cluster.get_template()),
                    "size": cluster.size,
                })
            
            clusters.sort(key=lambda x: x["size"], reverse=True)
            
            return {
                "total_clusters": len(clusters),
                "clusters": clusters[:30],
            }
        except ImportError:
            logger.warning("drain3 not installed, falling back to pattern analysis")
            return HeroLogAnalyzer.pattern_analysis(log_entries)


# ─────────────────────────────────────────
#  Trace Analysis (Hero P95/P99 + WeRCA Window)
# ─────────────────────────────────────────

class HeroTraceAnalyzer:
    """Hero-style trace latency analysis with WeRCA time-window comparison."""

    @staticmethod
    def latency_analysis(traces: List[Dict]) -> Dict:
        """Analyze trace latency distribution and identify slow traces."""
        if not traces:
            return {"note": "no traces to analyze"}
        
        durations = []
        for t in traces:
            if isinstance(t, dict):
                dur = t.get("duration", t.get("total_duration_us", 0))
            else:
                dur = t
            durations.append(float(dur))
        
        if not durations:
            return {"note": "no duration data"}
        
        durations.sort()
        n = len(durations)
        
        p50 = durations[n // 2]
        p95 = durations[int(n * 0.95)]
        p99 = durations[int(n * 0.99)]
        mean = sum(durations) / n
        
        # Identify slow traces (> p95)
        slow_traces = [
            t for t in traces
            if isinstance(t, dict) and float(t.get("duration", t.get("total_duration_us", 0))) > p95
        ]
        
        return {
            "count": n,
            "mean_us": round(mean, 0),
            "p50_us": round(p50, 0),
            "p95_us": round(p95, 0),
            "p99_us": round(p99, 0),
            "max_us": round(durations[-1], 0),
            "slow_trace_count": len(slow_traces),
            "slow_traces": slow_traces[:10],
        }

    @staticmethod
    def window_comparison(
        before_traces: List[Dict],
        after_traces: List[Dict],
    ) -> Dict:
        """WeRCA-style before/after time-window latency comparison."""
        def _extract_durations(traces):
            return [float(t.get("duration", t.get("total_duration_us", 0)))
                    for t in traces if isinstance(t, dict)]
        
        before_dur = _extract_durations(before_traces)
        after_dur = _extract_durations(after_traces)
        
        if not before_dur or not after_dur:
            return {"note": "insufficient data for comparison"}
        
        before_mean = sum(before_dur) / len(before_dur)
        after_mean = sum(after_dur) / len(after_dur)
        
        change = (after_mean - before_mean) / (before_mean + 1e-10)
        
        return {
            "before_mean_us": round(before_mean, 0),
            "after_mean_us": round(after_mean, 0),
            "latency_change_pct": round(change * 100, 1),
            "degradation": change > 0.2,
            "severity": "critical" if change > 1.0 else "high" if change > 0.5 else "medium" if change > 0.2 else "low",
        }


# ─────────────────────────────────────────
#  Cross-Signal Correlation (Hero)
# ─────────────────────────────────────────

class HeroCrossSignalCorrelator:
    """Hero-style cross-signal correlation analysis across log/metric/trace/event."""

    @staticmethod
    def build_anomaly_matrix(
        metric_anomalies: Dict[str, List],  # service -> list of anomalies
        log_anomalies: Dict[str, List],
        trace_anomalies: Dict[str, List],
        event_anomalies: Dict[str, List],
    ) -> Dict:
        """Build an anomaly matrix per service, compute composite scores."""
        all_services = set()
        all_services.update(metric_anomalies.keys())
        all_services.update(log_anomalies.keys())
        all_services.update(trace_anomalies.keys())
        all_services.update(event_anomalies.keys())
        
        matrix = {}
        for svc in all_services:
            m_count = len(metric_anomalies.get(svc, []))
            l_count = len(log_anomalies.get(svc, []))
            t_count = len(trace_anomalies.get(svc, []))
            e_count = len(event_anomalies.get(svc, []))
            
            # Signal presence (binary)
            signals = sum(1 for c in [m_count, l_count, t_count, e_count] if c > 0)
            
            # Composite score: weighted combination + multi-signal bonus
            score = (
                m_count * 0.3 +
                l_count * 0.25 +
                t_count * 0.25 +
                e_count * 0.2
            )
            # Multi-signal correlation bonus (Hero's key insight)
            if signals >= 3:
                score *= 1.5
            elif signals >= 2:
                score *= 1.2
            
            matrix[svc] = {
                "metric_anomalies": m_count,
                "log_anomalies": l_count,
                "trace_anomalies": t_count,
                "event_anomalies": e_count,
                "signal_count": signals,
                "composite_score": round(score, 2),
            }
        
        # Rank services by composite score
        ranked = sorted(matrix.items(), key=lambda x: x[1]["composite_score"], reverse=True)
        
        return {
            "service_count": len(ranked),
            "ranked_services": [
                {"service": svc, **data} for svc, data in ranked
            ],
            "top_suspect": ranked[0][0] if ranked else None,
        }
