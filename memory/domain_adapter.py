"""
AgenticSRE Domain Adapter
Load and switch domain profiles (Kubernetes, Linux, etc.) for cross-system generalization.
SOW: "通过领域适配、提示词优化等提升运维智能体的泛化性，实现跨系统的智能运维"
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class DomainProfile:
    """A domain-specific configuration profile."""
    domain_name: str = "kubernetes"
    agent_context_hints: Dict[str, str] = field(default_factory=dict)
    log_error_keywords: List[str] = field(default_factory=list)
    event_patterns: List[str] = field(default_factory=list)
    thresholds: Dict[str, float] = field(default_factory=dict)


class DomainAdapter:
    """
    Loads domain profiles from YAML files and provides the active profile.
    Supports auto-detection of the runtime environment.

    Usage:
        adapter = DomainAdapter.from_config()
        profile = adapter.get_active_profile()
        hint = profile.agent_context_hints.get("metric_agent", "")
    """

    def __init__(
        self,
        profiles_dir: Optional[str] = None,
        active_profile: str = "kubernetes",
        auto_detect: bool = False,
    ):
        self._profiles: Dict[str, DomainProfile] = {}
        self._active = active_profile

        # Resolve profiles directory
        if profiles_dir:
            self._profiles_dir = Path(profiles_dir)
        else:
            self._profiles_dir = Path(__file__).resolve().parent.parent / "configs" / "domains"

        self._load_all_profiles()

        if auto_detect:
            detected = self._auto_detect()
            if detected:
                self._active = detected

    @classmethod
    def from_config(cls) -> "DomainAdapter":
        """Create from global AppConfig."""
        from configs.config_loader import get_config
        cfg = get_config()
        return cls(
            profiles_dir=cfg.domain.profiles_dir or None,
            active_profile=cfg.domain.active_profile,
            auto_detect=cfg.domain.auto_detect,
        )

    def _load_all_profiles(self):
        """Load all YAML profiles from the profiles directory."""
        if not self._profiles_dir.is_dir():
            logger.warning("Domain profiles directory not found: %s", self._profiles_dir)
            self._profiles["kubernetes"] = DomainProfile(domain_name="kubernetes")
            return

        for yaml_file in sorted(self._profiles_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8")) or {}
                name = data.get("domain_name", yaml_file.stem)
                self._profiles[name] = DomainProfile(
                    domain_name=name,
                    agent_context_hints=data.get("agent_context_hints", {}),
                    log_error_keywords=data.get("log_error_keywords", []),
                    event_patterns=data.get("event_patterns", []),
                    thresholds=data.get("thresholds", {}),
                )
                logger.debug("Loaded domain profile: %s from %s", name, yaml_file.name)
            except Exception as e:
                logger.warning("Failed to load domain profile %s: %s", yaml_file, e)

        if not self._profiles:
            self._profiles["kubernetes"] = DomainProfile(domain_name="kubernetes")

    def _auto_detect(self) -> Optional[str]:
        """Auto-detect domain based on available system tools."""
        if shutil.which("kubectl"):
            return "kubernetes"
        if shutil.which("systemctl"):
            return "generic_linux"
        return None

    def get_active_profile(self) -> DomainProfile:
        """Return the currently active domain profile."""
        if self._active in self._profiles:
            return self._profiles[self._active]
        logger.warning("Active profile '%s' not found, falling back to first available", self._active)
        return next(iter(self._profiles.values()))

    def set_active(self, name: str) -> bool:
        """Switch the active domain profile."""
        if name in self._profiles:
            self._active = name
            logger.info("Switched domain profile to: %s", name)
            return True
        logger.warning("Domain profile '%s' not found", name)
        return False

    def list_profiles(self) -> List[str]:
        """Return all available profile names."""
        return list(self._profiles.keys())

    def get_profile(self, name: str) -> Optional[DomainProfile]:
        """Get a specific profile by name."""
        return self._profiles.get(name)
