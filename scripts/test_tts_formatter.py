from __future__ import annotations

from dialogue_manager.output.annotation_parser import parse_annotated_response
from dialogue_manager.tts.formatter import build_tts_api_text


def main() -> None:
    annotated = (
        "[emotion: neutre] Welcome *face: smile*. "
        "[pause: 0.5] Could you please introduce yourself? "
        "*gesture: deictic_you*"
    )

    output = parse_annotated_response(annotated)

    print("Annotated:")
    print(output.annotated_response)

    print("\nClean TTS text:")
    print(output.tts_text)

    print("\nTTS API text:")
    print(build_tts_api_text(output))


if __name__ == "__main__":
    main()