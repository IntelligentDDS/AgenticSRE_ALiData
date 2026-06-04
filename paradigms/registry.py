"""
Paradigm Registry
Provides @register_paradigm decorator and get_paradigm / list_paradigms factory functions.
"""

import logging
from typing import Dict, List, Optional, Type

from paradigms.base import ParadigmBase

logger = logging.getLogger(__name__)

# ── Global registry ──
_PARADIGM_REGISTRY: Dict[str, Type[ParadigmBase]] = {}


def register_paradigm(cls: Type[ParadigmBase]) -> Type[ParadigmBase]:
    """
    Class decorator that registers a paradigm by its .name attribute.

    Usage:
        @register_paradigm
        class MyParadigm(ParadigmBase):
            name = "my_paradigm"
    """
    name = getattr(cls, "name", None)
    if not name or name == "base":
        raise ValueError(f"Paradigm class {cls.__name__} must define a non-base 'name' attribute")
    _PARADIGM_REGISTRY[name] = cls
    logger.debug(f"Registered paradigm: {name} ({cls.__name__})")
    return cls


def get_paradigm(name: str) -> Type[ParadigmBase]:
    """Look up a paradigm class by name. Raises KeyError if not found."""
    if name not in _PARADIGM_REGISTRY:
        available = ", ".join(sorted(_PARADIGM_REGISTRY.keys()))
        raise KeyError(f"Unknown paradigm '{name}'. Available: {available}")
    return _PARADIGM_REGISTRY[name]


def list_paradigms() -> List[Dict[str, str]]:
    """Return a list of all registered paradigms with name and description."""
    return [
        {"name": cls.name, "description": cls.description}
        for cls in _PARADIGM_REGISTRY.values()
    ]


def paradigm_names() -> List[str]:
    """Return sorted list of registered paradigm names."""
    return sorted(_PARADIGM_REGISTRY.keys())
