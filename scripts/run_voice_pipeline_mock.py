from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from dialogue_manager.core.pipeline import DialoguePipeline
from dialogue_manager.engagement.adapter import StaticEngagementAnalyzer
from dialogue_manager.llm.mock_client import MockLLMClient
from dialogue_manager.stt.audio_io import list_input_devices, record_microphone_to_file
from dialogue_manager.stt.whisper_local import WhisperLocalSTT


def print_output(response_text: str, tts_annotations, unreal_actions) -> None:
    print("\n=== Dialogue Manager Output ===")
    print(f"Agent: {response_text}")

    print("\nTTS annotations:")
    for annotation in tts_annotations:
        print(annotation.model_dump())

    print("\nUnreal actions:")
    for action in unreal_actions:
        print(action.model_dump())

    print("=" * 32)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--device-index", type=int, default=None)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--model", type=str, default="medium.en")
    parser.add_argument("--compute-type", type=str, default="int8")

    args = parser.parse_args()

    if args.list_devices:
        list_input_devices()
        return

    print("Loading local Whisper STT...")
    stt = WhisperLocalSTT(
        model_name=args.model,
        device="cpu",
        compute_type=args.compute_type,
        language="en",
    )
    print("Whisper STT loaded.")

    pipeline = DialoguePipeline(
        engagement_analyzer=StaticEngagementAnalyzer(),
        llm_client=MockLLMClient(),
    )

    print("\nVoice pipeline ready.")
    print("Press ENTER to record a turn.")
    print("Type q + ENTER to quit.\n")

    while True:
        command = input("Press ENTER to speak, or q to quit: ").strip().lower()

        if command == "q":
            print("Exiting.")
            break

        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "user_turn.wav"

            record_microphone_to_file(
                output_path=audio_path,
                duration_seconds=args.duration,
                sample_rate=args.sample_rate,
                device_index=args.device_index,
            )

            transcription = stt.transcribe_file(audio_path)

        print("\n=== Whisper Transcription ===")
        print(transcription.text if transcription.text else "[EMPTY]")
        print("=============================")

        if not transcription.text:
            print("No text detected. Skipping dialogue turn.\n")
            continue

        output = pipeline.process_text_turn(transcription.text)

        print_output(
            response_text=output.response_text,
            tts_annotations=output.tts_annotations,
            unreal_actions=output.unreal_actions,
        )

        print(f"\nDialogue turn count: {pipeline.state.turn_count}\n")


if __name__ == "__main__":
    main()