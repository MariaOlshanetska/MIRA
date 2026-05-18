from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from dialogue_manager.core.pipeline import DialoguePipeline
from dialogue_manager.engagement.adapter import StaticEngagementAnalyzer
from dialogue_manager.llm.qwen_client import QwenLLMClient
from dialogue_manager.stt.audio_io import list_input_devices, record_microphone_to_file
from dialogue_manager.stt.whisper_local import WhisperLocalSTT


def print_dialogue_output(output) -> None:
    print("\n" + "=" * 60)
    print("DIALOGUE MANAGER OUTPUT")
    print("=" * 60)

    print("\nAnnotated response:")
    print(output.annotated_response)

    print("\nClean TTS text:")
    print(output.tts_text)

    print("\nTTS annotations:")
    if output.tts_annotations:
        for annotation in output.tts_annotations:
            print(annotation.model_dump())
    else:
        print("[none]")

    print("\nUnreal annotations:")
    if output.unreal_annotations:
        for annotation in output.unreal_annotations:
            print(annotation.model_dump())
    else:
        print("[none]")

    if output.debug:
        print("\nDebug:")
        print(output.debug)

    print("\n" + "=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--device-index", type=int, default=None)

    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument("--sample-rate", type=int, default=16000)

    parser.add_argument("--whisper-model", type=str, default="medium.en")
    parser.add_argument("--whisper-device", type=str, default="cpu")
    parser.add_argument("--whisper-compute-type", type=str, default="int8")

    args = parser.parse_args()

    if args.list_devices:
        list_input_devices()
        return

    print("\nLoading Whisper...")
    print(f"Model: {args.whisper_model}")
    print(f"Device: {args.whisper_device}")
    print(f"Compute type: {args.whisper_compute_type}")

    stt = WhisperLocalSTT(
        model_name=args.whisper_model,
        device=args.whisper_device,
        compute_type=args.whisper_compute_type,
        language="en",
    )

    print("Whisper loaded.")

    print("\nLoading Qwen client...")
    llm_client = QwenLLMClient()
    print(f"Qwen endpoint: {llm_client.chat_completions_url}")
    print(f"Qwen model: {llm_client.model}")

    pipeline = DialoguePipeline(
        engagement_analyzer=StaticEngagementAnalyzer(),
        llm_client=llm_client,
    )

    print("\nVoice dialogue pipeline ready.")
    print("Press ENTER to record one user turn.")
    print("Type q + ENTER to quit.")
    print("Speak in English.\n")

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

            print("\nTranscribing with Whisper...")
            transcription = stt.transcribe_file(audio_path)

        print("\n" + "-" * 60)
        print("WHISPER TRANSCRIPTION")
        print("-" * 60)
        print(transcription.text if transcription.text else "[EMPTY]")
        print("-" * 60)

        if not transcription.text:
            print("No speech detected. Skipping this turn.\n")
            continue

        print("\nSending turn to dialogue pipeline / Qwen...")

        try:
            output = pipeline.process_text_turn(transcription.text)
        except Exception as exc:
            print("\nERROR while processing dialogue turn:")
            print(exc)
            print(
                "\nMost likely causes: Qwen did not return a parseable annotated response, "
                "the server is unreachable, or the prompt format needs tightening."
            )
            continue

        print_dialogue_output(output)

        print(f"\nDialogue turn count: {pipeline.state.turn_count}\n")


if __name__ == "__main__":
    main()