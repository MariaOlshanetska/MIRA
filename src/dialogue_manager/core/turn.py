from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from dialogue_manager.engagement.types import EngagementState
from dialogue_manager.output.schemas import DialogueManagerOutput


class UserTurnInput(BaseModel):
    """
    Input received from the user in one conversational turn.

    At the beginning we may only have text. Later this can also include
    an audio path, timestamps, ASR confidence, etc.
    """

    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    user_text: str
    audio_path: Path | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)


class DialogueTurn(BaseModel):
    """
    Complete turn record: user input + engagement + final system output.
    """

    user_input: UserTurnInput
    engagement: EngagementState
    output: DialogueManagerOutput

    raw_llm_output: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)