from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from dialogue_manager.core.state import DialogueState
from dialogue_manager.core.turn import UserTurnInput
from dialogue_manager.engagement.types import EngagementState
from dialogue_manager.output.schemas import DialogueManagerOutput


class LLMRequest(BaseModel):
    """
    Structured input for the LLM module.
    """

    user_input: UserTurnInput
    engagement: EngagementState
    state: DialogueState

    system_prompt: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """
    Raw and parsed LLM response.

    The raw text is useful because the model may return malformed JSON.
    The parsed output is what the rest of the system should consume.
    """

    raw_text: str
    parsed_output: DialogueManagerOutput | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMClient(ABC):
    """
    Abstract interface for any LLM backend.

    Later we will implement this with the lab's Qwen API.
    """

    @abstractmethod
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError