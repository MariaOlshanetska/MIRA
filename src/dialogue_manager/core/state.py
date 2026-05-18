from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from dialogue_manager.core.turn import DialogueTurn
from dialogue_manager.engagement.types import EngagementState


class DialogueState(BaseModel):
    """
    Persistent state for a dialogue session.

    This should stay independent from Whisper, Qwen, Unreal, and TTS.
    """

    session_id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=datetime.utcnow)

    turns: list[DialogueTurn] = Field(default_factory=list)
    last_engagement: EngagementState | None = None

    variables: dict[str, Any] = Field(default_factory=dict)

    def add_turn(self, turn: DialogueTurn) -> None:
        self.turns.append(turn)
        self.last_engagement = turn.engagement

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def recent_turns(self, limit: int = 5) -> list[DialogueTurn]:
        return self.turns[-limit:]

    def recent_history_as_text(self, limit: int = 5) -> str:
        """
        Compact text representation for prompts.
        """

        recent = self.recent_turns(limit=limit)
        lines: list[str] = []

        for turn in recent:
            lines.append(f"User: {turn.user_input.user_text}")
            lines.append(f"Agent: {turn.output.response_text}")

        return "\n".join(lines)