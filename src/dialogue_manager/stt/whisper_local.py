from __future__ import annotations

from pathlib import Path
from typing import Any

from faster_whisper import WhisperModel

from dialogue_manager.stt.base import SpeechToTextEngine, TranscriptionResult


class WhisperLocalSTT(SpeechToTextEngine):
    """
    Local Whisper implementation using faster-whisper.

    For now we force English because the project dialogue is expected
    to happen in English.
    """

    def __init__(
        self,
        model_name: str = "medium.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language

        self.model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
        )

    def transcribe_file(self, audio_path: Path) -> TranscriptionResult:
        segments_generator, info = self.model.transcribe(
            str(audio_path),
            language=self.language,
            task="transcribe",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )

        text_parts: list[str] = []
        segments: list[dict[str, Any]] = []

        for segment in segments_generator:
            segment_text = segment.text.strip()

            if segment_text:
                text_parts.append(segment_text)

            segments.append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment_text,
                }
            )

        final_text = " ".join(text_parts).strip()

        return TranscriptionResult(
            text=final_text,
            language=getattr(info, "language", self.language) or self.language,
            confidence=getattr(info, "language_probability", None),
            segments=segments,
            metadata={
                "model_name": self.model_name,
                "device": self.device,
                "compute_type": self.compute_type,
            },
        )