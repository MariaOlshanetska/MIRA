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

    def _compact_system_event_as_text(self, turn: DialogueTurn) -> str | None:
        """
        Render internal system events compactly for the prompt.

        We deliberately do not expose the full synthetic system-event user text,
        because Qwen may treat it as a real candidate utterance. We also avoid
        replaying the full opening response, because that makes the next turn
        more likely to repeat Aera's introduction.
        """

        user_text = turn.user_input.user_text.strip()
        turn_type = turn.metadata.get("type")

        if user_text.startswith("[system_event: interview_start]"):
            return (
                "Session state: Aera has already opened the interview, "
                "introduced herself and CCIA, framed the conversation as relaxed, "
                "and asked the candidate how they are doing. Do not repeat the opening."
            )

        if user_text.startswith("[system_event: interview_start_fallback]"):
            return (
                "Session state: Aera has already opened the interview with a fallback opening. "
                "Do not repeat the opening, self-introduction, or CCIA framing."
            )

        if turn_type == "agent_opening":
            return (
                "Session state: Aera has already introduced herself and started the interview. "
                "Do not repeat the opening."
            )

        if turn_type == "engagement_repair" or "Engagement dropped" in user_text:
            return (
                "System event: Aera made a brief interaction-repair move because engagement dropped "
                "while she was speaking. Continue from the candidate's next response."
            )

        if user_text.startswith("[system_event"):
            return "System event: internal dialogue state updated."

        return None

    def recent_history_as_text(self, limit: int = 5) -> str:
        """
        Compact text representation for prompts.

        System events are summarized instead of shown verbatim. This prevents the
        LLM from repeating opening instructions or treating internal events as
        candidate speech.
        """

        recent = self.recent_turns(limit=limit)
        lines: list[str] = []

        for turn in recent:
            system_line = self._compact_system_event_as_text(turn)
            if system_line is not None:
                if system_line not in lines:
                    lines.append(system_line)
                continue

            lines.append(f"Candidate: {turn.user_input.user_text}")
            lines.append(f"Aera: {turn.output.response_text}")

        return "\n".join(lines)
