from __future__ import annotations

from abc import ABC, abstractmethod

from dialogue_manager.core.turn import UserTurnInput
from dialogue_manager.core.state import DialogueState
from dialogue_manager.engagement.types import EngagementState


class EngagementAnalyzer(ABC):
    """
    Abstract interface for the engagement module.

    The dialogue manager should query the latest numeric engagement score only
    when it is about to build the LLM prompt. Conversational adaptation policy
    belongs in the prompt, not in the analyzer.
    """

    @abstractmethod
    def get_latest_score(self) -> float | None:
        """
        Return the latest realtime engagement score in [0, 1], or None if no
        score is available yet.
        """
        raise NotImplementedError

    def analyze(
        self,
        user_input: UserTurnInput,
        state: DialogueState,
    ) -> EngagementState:
        """
        Legacy compatibility wrapper.

        New code should use get_latest_score() and build a score-only
        EngagementState in the dialogue pipeline. This method intentionally does
        not add dialogue policy, levels, or repair instructions.
        """
        score = self.get_latest_score()
        ready = score is not None

        if score is None:
            score = 0.5

        score = max(0.0, min(1.0, float(score)))

        return EngagementState(
            score=score,
            summary=f"Realtime engagement score: {score:.3f}.",
            metadata={
                "source": "engagement_analyzer_legacy_analyze",
                "ready": ready,
            },
        )
