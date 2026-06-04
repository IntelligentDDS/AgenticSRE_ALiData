"""
AgenticSRE Anomaly Detection Tool
Statistical anomaly detection: Z-score, IQR, Spectral Residual, Static Threshold.
"""

import math
import logging
from typing import Any, Dict, List, Optional

from tools.base_tool import SRETool, ToolResult

logger = logging.getLogger(__name__)


class AnomalyDetectionTool(SRETool):
    """Multi-method statistical anomaly detection for time-series data."""

    name = "anomaly_detection"
    description = "Detect anomalies in time-series data using statistical methods"

    def _execute(self, values: List[float], method: str = "zscore",
                 threshold: float = 3.0, labels: Optional[List[str]] = None) -> ToolResult:
        if not values:
            return ToolResult(success=False, error="No values provided")

        if method == "zscore":
            anomalies = self._zscore(values, threshold)
        elif method == "iqr":
            anomalies = self._iqr(values)
        elif method == "static":
            anomalies = self._static_threshold(values, threshold)
        elif method == "rate_change":
            anomalies = self._rate_change(values, threshold)
        else:
            return ToolResult(success=False, error=f"Unknown method: {method}")

        return ToolResult(success=True, data={
            "method": method,
            "total_points": len(values),
            "anomaly_count": len(anomalies),
            "anomaly_ratio": len(anomalies) / len(values) if values else 0,
            "anomalies": anomalies[:50],  # limit
        })

    def _zscore(self, values: List[float], threshold: float) -> List[Dict]:
        n = len(values)
        if n < 3:
            return []
        mean = sum(values) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
        if std == 0:
            return []
        
        anomalies = []
        for i, v in enumerate(values):
            z = abs(v - mean) / std
            if z > threshold:
                anomalies.append({
                    "index": i, "value": v,
                    "zscore": round(z, 2),
                    "severity": "critical" if z > threshold * 1.5 else "warning"
                })
        return anomalies

    def _iqr(self, values: List[float]) -> List[Dict]:
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        q1 = sorted_vals[n // 4]
        q3 = sorted_vals[3 * n // 4]
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        
        return [
            {"index": i, "value": v, "bound": "lower" if v < lower else "upper"}
            for i, v in enumerate(values)
            if v < lower or v > upper
        ]

    def _static_threshold(self, values: List[float], threshold: float) -> List[Dict]:
        return [
            {"index": i, "value": v}
            for i, v in enumerate(values)
            if v > threshold
        ]

    def _rate_change(self, values: List[float], threshold: float) -> List[Dict]:
        """Detect sudden rate changes (WeRCA-style)."""
        anomalies = []
        for i in range(1, len(values)):
            if values[i - 1] != 0:
                rate = abs(values[i] - values[i - 1]) / abs(values[i - 1])
            else:
                rate = abs(values[i])
            if rate > threshold:
                anomalies.append({
                    "index": i, "value": values[i],
                    "prev_value": values[i - 1],
                    "rate_change": round(rate, 3),
                })
        return anomalies

    def _parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "values": {"type": "array", "items": {"type": "number"}},
                "method": {"type": "string", "enum": ["zscore", "iqr", "static", "rate_change"]},
                "threshold": {"type": "number", "default": 3.0},
            },
            "required": ["values"]
        }
