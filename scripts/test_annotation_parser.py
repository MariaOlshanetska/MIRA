from __future__ import annotations

from dialogue_manager.output.annotation_parser import parse_annotated_response


def main() -> None:
    annotated = (
        "[neutral] [clear_throat] Welcome *face: smile*. "
        "Today's goal is having a first impression of each other. "
        "Could you please start by introducing yourself? *gesture: deictic_you*"
    )

    output = parse_annotated_response(annotated)

    print("\nAnnotated response:")
    print(output.annotated_response)

    print("\nTTS text:")
    print(output.tts_text)

    print("\nTTS annotations:")
    for annotation in output.tts_annotations:
        print(annotation.model_dump())

    print("\nUnreal annotations:")
    for annotation in output.unreal_annotations:
        print(annotation.model_dump())


if __name__ == "__main__":
    main()