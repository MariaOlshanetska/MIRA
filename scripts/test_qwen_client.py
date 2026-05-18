from __future__ import annotations

from dialogue_manager.core.pipeline import DialoguePipeline
from dialogue_manager.engagement.adapter import StaticEngagementAnalyzer
from dialogue_manager.llm.qwen_client import QwenLLMClient


def main() -> None:
    pipeline = DialoguePipeline(
        engagement_analyzer=StaticEngagementAnalyzer(),
        llm_client=QwenLLMClient(),
    )

    print("Qwen dialogue pipeline ready.")
    print("Type a message in English. Press Ctrl+C to stop.\n")

    while True:
        user_text = input("You: ").strip()

        if not user_text:
            continue

        try:
            output = pipeline.process_text_turn(user_text)
        except Exception as exc:
            print(f"\nERROR: {exc}\n")
            continue

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

        print(f"\nTurn count: {pipeline.state.turn_count}")
        print("-" * 50)


if __name__ == "__main__":
    main()