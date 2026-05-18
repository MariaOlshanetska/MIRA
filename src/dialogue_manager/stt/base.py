from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class TranscriptionResult(BaseModel):
    """
    Result produced by the speech-to-text module.
    """

    text: str
    language: str = "en"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    segments: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SpeechToTextEngine(ABC):
    """
    Abstract interface for any speech-to-text backend.

    Later we will implement this with faster-whisper.
    """

    @abstractmethod
    def transcribe_file(self, audio_path: Path) -> TranscriptionResult:
        raise NotImplementedError