"""
AgenticSRE ActionStack
Thread-safe undo stack for remediation rollback capability.
"""

import logging
import threading
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class Action:
    """A recorded remediation action with rollback capability."""
    action_id: str
    description: str
    command: str
    rollback_command: str
    timestamp: str = ""
    status: str = "pending"     # pending | executed | rolled_back | failed
    result: str = ""
    rollback_result: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


class ActionStack:
    """Thread-safe LIFO stack of remediation actions with rollback support."""

    def __init__(self, max_depth: int = 10):
        self._stack: List[Action] = []
        self._lock = threading.Lock()
        self.max_depth = max_depth

    def push(self, action: Action) -> None:
        """Push an action onto the stack."""
        with self._lock:
            if len(self._stack) >= self.max_depth:
                logger.warning(f"ActionStack full ({self.max_depth}), dropping oldest")
                self._stack.pop(0)
            self._stack.append(action)
            logger.info(f"ActionStack: pushed [{action.action_id}] {action.description}")

    def pop(self) -> Optional[Action]:
        """Pop the most recent action."""
        with self._lock:
            return self._stack.pop() if self._stack else None

    def peek(self) -> Optional[Action]:
        """View the most recent action without removing it."""
        with self._lock:
            return self._stack[-1] if self._stack else None

    def rollback_last(self, executor: Callable[[str], str]) -> Optional[Dict]:
        """Roll back the most recent action using the provided executor."""
        action = self.pop()
        if action is None:
            return None
        
        if not action.rollback_command:
            logger.warning(f"No rollback command for [{action.action_id}]")
            action.status = "failed"
            return {"action": action.action_id, "status": "no_rollback_command"}

        try:
            result = executor(action.rollback_command)
            action.status = "rolled_back"
            action.rollback_result = str(result)
            logger.info(f"ActionStack: rolled back [{action.action_id}]")
            return {"action": action.action_id, "status": "rolled_back", "result": result}
        except Exception as e:
            action.status = "failed"
            action.rollback_result = str(e)
            logger.error(f"ActionStack: rollback failed [{action.action_id}]: {e}")
            return {"action": action.action_id, "status": "failed", "error": str(e)}

    def rollback_all(self, executor: Callable[[str], str]) -> List[Dict]:
        """Roll back all actions in LIFO order."""
        results = []
        while self._stack:
            r = self.rollback_last(executor)
            if r:
                results.append(r)
        return results

    def list_actions(self) -> List[Dict]:
        """List all actions in the stack."""
        with self._lock:
            return [
                {
                    "id": a.action_id,
                    "description": a.description,
                    "command": a.command,
                    "status": a.status,
                    "timestamp": a.timestamp,
                }
                for a in reversed(self._stack)
            ]

    def __len__(self) -> int:
        with self._lock:
            return len(self._stack)

    def clear(self) -> None:
        with self._lock:
            self._stack.clear()
