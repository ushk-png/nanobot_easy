"""Workflow configuration DTOs."""
from __future__ import annotations

from pydantic import Field

from nanobot.config_base import Base


class WorkflowToolConfig(Base):
    enabled: bool = False
    sync_wait_seconds: int = Field(default=60, ge=0, le=600)
