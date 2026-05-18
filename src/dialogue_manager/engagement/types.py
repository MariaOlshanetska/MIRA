from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


EngagementLevel = Literal["very_low", "low", "medium", "high", "very_high"]


class EngagementSignal(BaseModel):
    """
    Low-level engagement information.

    This can come from your engagement module, computer vision,
    interaction history, voice activity, latency, gaze, etc.
    """

    name: str
    value: float | str | bool
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class EngagementState(BaseModel):
    """
    Aggregated engagement state for the current user/turn.
    """

    level: EngagementLevel = "medium"
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    signals: list[EngagementSignal] = Field(default_factory=list)
    summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)