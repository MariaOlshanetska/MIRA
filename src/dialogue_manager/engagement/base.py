from __future__ import annotations

from abc import ABC, abstractmethod

from dialogue_manager.core.turn import UserTurnInput
from dialogue_manager.core.state import DialogueState
from dialogue_manager.engagement.types import EngagementState


class EngagementAnalyzer(ABC):
    """
    Abstract interface for the engagement module.
    """

    @abstractmethod
    def analyze(
        self,
        user_input: UserTurnInput,
        state: DialogueState,
    ) -> EngagementState:
        raise NotImplementedError