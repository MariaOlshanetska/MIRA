from __future__ import annotations

import re

from dialogue_manager.output.schemas import DialogueManagerOutput


ALLOWED_TTS_EMOTIONS = {
    "happiness",
    "angry",
    "sad",
    "neutre",
    "whisper",
}


def _normalise_pause_seconds(value: str) -> float:
    """
    Accept seconds, e.g. 0.5.

    Also accepts old millisecond-style values, e.g. 500 -> 0.5.
    """

    seconds = float(value.strip())

    if seconds > 10:
        seconds = seconds / 1000.0

    return max(0.0, seconds)


def _parse_annotation_content(content: str) -> tuple[str, str | None]:
    content = content.strip()

    if ":" not in content:
        return content.lower(), None

    name, value = content.split(":", maxsplit=1)
    return name.strip().lower(), value.strip()


def _tts_annotation_to_api_tag(content: str) -> str:
    name, value = _parse_annotation_content(content)

    if name == "emotion":
        if value is None:
            return ""

        emotion = value.strip().lower()

        if emotion not in ALLOWED_TTS_EMOTIONS:
            return ""

        return f"<emotion({emotion})>"

    if name in ALLOWED_TTS_EMOTIONS and value is None:
        return f"<emotion({name})>"

    if name in {"pause", "silence"}:
        if value is None:
            return ""

        try:
            seconds = _normalise_pause_seconds(value)
        except ValueError:
            return ""

        return f"<silence({seconds:g})>"

    if name == "burst":
        if value is None:
            return ""

        burst_name = value.strip()
        if not burst_name:
            return ""

        return f"<burst({burst_name})>"

    # Unknown TTS annotations are removed from the API text.
    return ""


def build_tts_api_text(output: DialogueManagerOutput) -> str:
    """
    Convert the LLM annotated response into the exact format expected by the TTS API.

    Example input:
        [emotion: sad] Hello [pause: 0.5] there *face: smile*.

    Example output:
        <emotion(sad)> Hello <silence(0.5)> there.
    """

    text = output.annotated_response

    # Remove Unreal annotations before sending to TTS.
    text = re.sub(r"\*[^*]+\*", "", text)

    # Convert square-bracket TTS annotations.
    text = re.sub(
        r"\[([^\[\]]+)\]",
        lambda match: _tts_annotation_to_api_tag(match.group(1)),
        text,
    )

    # Clean awkward spaces.
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)

    return text.strip()