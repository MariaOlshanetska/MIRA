from __future__ import annotations

from dialogue_manager.core.state import DialogueState
from dialogue_manager.core.turn import UserTurnInput
from dialogue_manager.engagement.base import EngagementAnalyzer
from dialogue_manager.engagement.types import EngagementSignal, EngagementState


class StaticEngagementAnalyzer(EngagementAnalyzer):
    """
    Temporary engagement analyzer.

    Later this will be replaced by the real engagement module.
    """

    def analyze(
        self,
        user_input: UserTurnInput,
        state: DialogueState,
    ) -> EngagementState:
        word_count = len(user_input.user_text.split())

        if word_count <= 2:
            level = "low"
            score = 0.35
        elif word_count <= 8:
            level = "medium"
            score = 0.6
        else:
            level = "high"
            score = 0.8

        return EngagementState(
            level=level,
            score=score,
            signals=[
                EngagementSignal(
                    name="word_count",
                    value=word_count,
                    confidence=1.0,
                )
            ],
            summary=f"Temporary engagement estimate based on word count: {word_count}.",
        )