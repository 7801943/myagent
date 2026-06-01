"""Compatibility shim for the old hook module.

New code should import from :mod:`myagent.core.events`.
"""

from myagent.core.events import (
    EventBus as HookManager,
    EventHandle as HookHandle,
    ExecutionContext as HookContext,
)

__all__ = ["HookContext", "HookManager", "HookHandle"]
