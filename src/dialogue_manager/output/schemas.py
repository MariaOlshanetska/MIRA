from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TTSAnnotation(BaseModel):
    """
    Annotation for the TTS system.

    These annotations come from square brackets in the LLM response.

    Example:
        [neutral]
        [clear_throat]
        [pause: 500]
    """

    name: str = Field(..., description="Annotation name, e.g. neutral, pause, clear_throat.")
    value: str | None = Field(default=None, description="Optional value, e.g. 500 in [pause: 500].")
    position: int = Field(..., ge=0, description="Character position in the clean TTS text.")
    raw: str = Field(..., description="Original raw annotation, e.g. [neutral].")


class UnrealAnnotation(BaseModel):
    """
    Annotation for Unreal Engine.

    These annotations come from asterisks in the LLM response.

    Example:
        *face: smile*
        *gesture: deictic_you*
        *gaze: look_at_user*
    """

    channel: str = Field(..., description="Unreal channel, e.g. face, gesture, gaze, posture.")
    name: str = Field(..., description="Action/expression name from the Unreal library.")
    position: int = Field(..., ge=0, description="Character position in the clean TTS text.")
    raw: str = Field(..., description="Original raw annotation, e.g. *gesture: deictic_you*.")
    parameters: dict[str, Any] = Field(default_factory=dict)


class DialogueManagerOutput(BaseModel):
    """
    Final structured output produced by the dialogue manager for one turn.

    The LLM should produce an annotated response. The system then parses it
    into clean TTS text, TTS annotations, and Unreal annotations.
    """

    annotated_response: str
    tts_text: str
    tts_annotations: list[TTSAnnotation] = Field(default_factory=list)
    unreal_annotations: list[UnrealAnnotation] = Field(default_factory=list)

    debug: dict[str, Any] = Field(default_factory=dict)

    @property
    def response_text(self) -> str:
        """
        Backwards-compatible alias.

        Older scripts may still call output.response_text.
        """
        return self.tts_text

    @property
    def unreal_actions(self) -> list[UnrealAnnotation]:
        """
        Backwards-compatible alias.

        Older scripts may still call output.unreal_actions.
        """
        return self.unreal_annotations