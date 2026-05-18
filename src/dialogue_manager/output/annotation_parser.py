from __future__ import annotations

import re

from dialogue_manager.output.schemas import (
    DialogueManagerOutput,
    TTSAnnotation,
    UnrealAnnotation,
)


class AnnotationParseError(ValueError):
    pass


def _parse_name_value(content: str) -> tuple[str, str | None]:
    """
    Parse annotations such as:

        neutral
        pause: 500
        gesture: deictic_you
    """

    content = content.strip()

    if not content:
        raise AnnotationParseError("Empty annotation found.")

    if ":" not in content:
        return content, None

    name, value = content.split(":", maxsplit=1)
    return name.strip(), value.strip()


def _clean_tts_text(text: str) -> str:
    """
    Remove awkward whitespace after deleting annotations.

    Example:
        "Welcome . Hello  there" -> "Welcome. Hello there"
    """

    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def parse_annotated_response(annotated_response: str) -> DialogueManagerOutput:
    """
    Parse one LLM annotated response.

    Square brackets [] are TTS annotations.
    Asterisks ** are Unreal annotations.

    Example:
        [neutral] Welcome *face: smile*. Please introduce yourself. *gesture: deictic_you*
    """

    clean_chars: list[str] = []
    tts_annotations: list[TTSAnnotation] = []
    unreal_annotations: list[UnrealAnnotation] = []

    i = 0

    while i < len(annotated_response):
        char = annotated_response[i]

        if char == "[":
            end = annotated_response.find("]", i + 1)

            if end == -1:
                raise AnnotationParseError(
                    f"Unclosed TTS annotation starting at character {i}."
                )

            content = annotated_response[i + 1 : end]
            name, value = _parse_name_value(content)

            tts_annotations.append(
                TTSAnnotation(
                    name=name,
                    value=value,
                    position=len("".join(clean_chars)),
                    raw=annotated_response[i : end + 1],
                )
            )

            i = end + 1
            continue

        if char == "*":
            end = annotated_response.find("*", i + 1)

            if end == -1:
                raise AnnotationParseError(
                    f"Unclosed Unreal annotation starting at character {i}."
                )

            content = annotated_response[i + 1 : end]
            channel, value = _parse_name_value(content)

            if value is None:
                # Fallback for annotations like *smile*.
                # Later we should avoid this in the prompt and require *face: smile*.
                channel = "action"
                name = content.strip()
            else:
                name = value

            unreal_annotations.append(
                UnrealAnnotation(
                    channel=channel,
                    name=name,
                    position=len("".join(clean_chars)),
                    raw=annotated_response[i : end + 1],
                )
            )

            i = end + 1
            continue

        clean_chars.append(char)
        i += 1

    raw_tts_text = "".join(clean_chars)
    tts_text = _clean_tts_text(raw_tts_text)

    return DialogueManagerOutput(
        annotated_response=annotated_response,
        tts_text=tts_text,
        tts_annotations=tts_annotations,
        unreal_annotations=unreal_annotations,
    )