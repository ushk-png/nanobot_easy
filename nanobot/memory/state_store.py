"""Minimal task projection helpers for later phases.

Raw events remain the source of truth. This module only provides the Phase-1
schema location so future TASKS.md projection work has a stable home.
"""

from __future__ import annotations

TASK_STATUSES = frozenset({"OPEN", "IN_PROGRESS", "BLOCKED", "DONE", "CANCELLED"})
