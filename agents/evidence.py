"""Structured evidence helpers shared by RCA agents."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class EvidenceItem:
    """One normalized observation produced by an evidence agent."""

    signal_type: str
    service: str = ""
    source: str = ""
    observation: str = ""
    severity: str = "info"
    timestamp: str = ""
    supports: List[str] = field(default_factory=list)
    contradicts: List[str] = field(default_factory=list)
    raw_ref: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def make_evidence_item(
    signal_type: str,
    observation: str,
    *,
    service: str = "",
    source: str = "",
    severity: str = "info",
    timestamp: str = "",
    raw_ref: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return EvidenceItem(
        signal_type=signal_type,
        service=service,
        source=source,
        observation=observation,
        severity=severity,
        timestamp=timestamp,
        raw_ref=raw_ref or {},
    ).to_dict()

