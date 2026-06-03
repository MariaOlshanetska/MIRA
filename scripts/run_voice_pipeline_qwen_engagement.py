from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from dialogue_manager.core.pipeline import DialoguePipeline
from dialogue_manager.engagement.realtime_subprocess import RealtimeEngagementSubprocessAnalyzer
from dialogue_manager.engagement.types import EngagementState
from dialogue_manager.llm.qwen_client import QwenLLMClient
from dialogue_manager.stt.audio_io import list_input_devices, record_microphone_until_silence
from dialogue_manager.stt.whisper_local import WhisperLocalSTT
from dialogue_manager.tts.formatter import build_tts_api_text
from dialogue_manager.core.turn import DialogueTurn, UserTurnInput
from dialogue_manager.output.annotation_parser import parse_annotated_response

from dialogue_manager.tts.audio_player import play_wav_file
from dialogue_manager.tts.expressive_client import ExpressiveTTSClient


DEFAULT_ENGAGEMENT_URL = "http://10.10.200.182/engagement_maria/engagement_maria"

def build_opening_annotated_response(candidate_profession: str) -> str:
    return (
        "[emotion: happiness] *face: FACE_SMILE_LOW* Hi, welcome. "
        "It is really nice to finally meet you. "
        "[silence: 0.3] *gesture: DEICTIC_ME_1* My name is Aera, and I will be guiding this first conversation today. "
        f"[silence: 0.3] *gesture: EXPLAIN_BEAT_1* This interview will be focused on your career as {candidate_profession}. "
        "[silence: 0.4] *gesture: DEICTIC_YOU_1* How are you doing today?"
    )

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

    # Audio for Whisper user-turn recording
    parser.add_argument("--device-index", type=int, default=None)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--silence-seconds", type=float, default=1.2)
    parser.add_argument("--silence-threshold", type=float, default=0.015)
    parser.add_argument("--min-record-seconds", type=float, default=1.0)

    # Whisper
    parser.add_argument("--whisper-model", type=str, default="medium.en")
    parser.add_argument("--whisper-device", type=str, default="cpu")
    parser.add_argument("--whisper-compute-type", type=str, default="int8")

    # Engagement recognizer
    parser.add_argument(
        "--engagement-script",
        type=str,
        default="scripts/realtime_multimodal_engagement.py",
    )
    parser.add_argument("--engagement-url", type=str, default=DEFAULT_ENGAGEMENT_URL)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--engagement-audio-device", default=None)
    parser.add_argument("--calib", type=float, default=5.0)
    parser.add_argument("--engagement-fps", type=float, default=3.0)
    parser.add_argument("--fusion-fps", type=float, default=3.0)
    parser.add_argument("--show-engagement-window", action="store_true")

    args = parser.parse_args()

    if args.list_devices:
        list_input_devices()
        return

    engagement = RealtimeEngagementSubprocessAnalyzer(
        script_path=Path(args.engagement_script),
        url=args.engagement_url,
        camera=args.camera,
        audio_device=args.engagement_audio_device,
        calib_seconds=args.calib,
        fps=args.engagement_fps,
        fusion_fps=args.fusion_fps,
        no_window=not args.show_engagement_window,
        overwrite_log=True,
    )

    print("\nRealtime engagement recognizer is ready to start.")
    print(f"Engagement URL: {args.engagement_url}")
    print(f"Camera index: {args.camera}")
    print(f"Engagement audio device: {args.engagement_audio_device}")
    print(f"\nCalibration will last {args.calib:.1f} seconds.")
    print("During calibration, please speak naturally for a few seconds.")
    print("For example: 'What is your location? What's the weather like today?'")
    print("The interview will start after the calibration.")
    print("\nPress ENTER when you are ready to start calibration.")
    input()

    print("\nStarting realtime engagement recognizer...")
    print("Calibrating now. Please speak naturally...\n")

    engagement.start()

    ready = engagement.wait_until_ready(timeout_seconds=args.calib + 15.0)

    if ready:
        print("\nEngagement calibration finished.")
        print("Engagement recognizer is now running continuously.")
        print("You can now press ENTER to speak to the dialogue manager.")

        latest_score = engagement.get_latest_score()
        if latest_score is not None:
            print(f"Initial engagement score: {latest_score:.3f}")

    else:
        print("WARNING: engagement recognizer is not ready yet.")
        print("The dialogue manager will use neutral engagement until values arrive.")

    print("\nBefore Aera starts, enter the candidate's profession or field.")
    print("Example: nurse, architect, primary school teacher, software developer, firefighter...")
    candidate_profession = input("Candidate profession / field: ").strip()

    if not candidate_profession:
        candidate_profession = "a general professional field"

    print(f"\nCandidate profession set to: {candidate_profession}")

    print("\nLoading Whisper...")
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

    print("\nLoading TTS client...")
    tts_client = ExpressiveTTSClient()
    print(f"TTS endpoint: {tts_client.url}")

    pipeline = DialoguePipeline(
        engagement_analyzer=engagement,
        llm_client=llm_client,
    )

    print("\nVoice dialogue pipeline ready.")
    print("Aera will start the interview.")
    print("Type q + ENTER to quit.")
    print("Speak in English after Aera's first question.\n")

    opening_annotated_response = build_opening_annotated_response(candidate_profession)
    opening_output = parse_annotated_response(opening_annotated_response)

    print_dialogue_output(opening_output)

    print("\nSending opening message to TTS API...")

    tts_output_path = (
        Path(tempfile.gettempdir())
        / "dialogue_manager"
        / "latest_tts_output.wav"
    )

    try:
        tts_api_text = build_tts_api_text(opening_output)

        print("\nTTS API text:")
        print(tts_api_text)

        wav_path = tts_client.synthesize_to_file(
            text=tts_api_text,
            output_path=tts_output_path,
        )

        print(f"TTS WAV saved to: {wav_path}")
        print("Playing Aera's opening message...")

        play_wav_file(wav_path)

    except Exception as exc:
        print("\nERROR while generating or playing opening TTS audio:")
        print(exc)

    # Store the opening in dialogue history so Qwen knows the interview has already started.
    opening_turn = DialogueTurn(
        user_input=UserTurnInput(
            user_text=(
                "[session_start]\n"
                f"Candidate profession / field: {candidate_profession}\n"
                "Aera must adapt the interview to this professional field. "
                "Do not assume the candidate is a computational linguist unless this profession was explicitly provided."
            )
        ),
        engagement=EngagementState(
            score=0.5,
            summary="Opening turn before candidate response; neutral engagement.",
            metadata={
                "source": "agent_opening",
                "ready": False,
            },
        ),
        output=opening_output,
        raw_llm_output=opening_annotated_response,
        metadata={
            "type": "agent_opening",
            "candidate_profession": candidate_profession,
        },
    )

    pipeline.state.add_turn(opening_turn)

    print("\nNow press ENTER to answer Aera.")

    engagement_warning_printed = False

    try:
        while True:
            if not engagement.is_running() and not engagement_warning_printed:
                print("\nWARNING: Engagement recognizer is no longer running.")
                print("This probably happened because the OpenCV window was closed or q was pressed.")
                print("The dialogue manager will keep using the last available engagement value.")
                print("Restart the script if you need live engagement again.\n")
                engagement_warning_printed = True

            current_score = engagement.get_latest_score()
            if current_score is None:
                score_text = "unknown"
            else:
                score_text = f"{current_score:.3f}"

            command = input(
                f"Current engagement={score_text}. Press ENTER to answer Aera, or q to quit: "
            ).strip().lower()

            if command == "q":
                print("Exiting.")
                break

            with tempfile.TemporaryDirectory() as temp_dir:
                audio_path = Path(temp_dir) / "user_turn.wav"

                record_microphone_until_silence(
                    output_path=audio_path,
                    sample_rate=args.sample_rate,
                    device_index=args.device_index,
                    silence_seconds=args.silence_seconds,
                    silence_threshold=args.silence_threshold,
                    min_record_seconds=args.min_record_seconds,
                    max_record_seconds=args.duration,
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

            engagement_score = engagement.get_latest_score()
            if engagement_score is None:
                print("Engagement score sent to prompt: unavailable -> using neutral 0.500")
            else:
                print(f"Engagement score sent to prompt: {engagement_score:.3f}")

            print("\nSending turn to dialogue pipeline / Qwen...")

            try:
                output = pipeline.process_text_turn(
                    transcription.text,
                    engagement_score=engagement_score,
                )

            print("\nSending turn to dialogue pipeline / Qwen...")

            try:
                output = pipeline.process_text_turn(transcription.text)
            except Exception as exc:
                print("\nERROR while processing dialogue turn:")
                print(exc)
                continue

            print_dialogue_output(output)

            print("\nSending clean TTS text to TTS API...")

            tts_output_path = (
                Path(tempfile.gettempdir())
                / "dialogue_manager"
                / "latest_tts_output.wav"
            )

            try:
                tts_api_text = build_tts_api_text(output)

                print("\nTTS API text:")
                print(tts_api_text)

                wav_path = tts_client.synthesize_to_file(
                    text=tts_api_text,
                    output_path=tts_output_path,
                )

                print(f"TTS WAV saved to: {wav_path}")
                print("Playing TTS audio...")

                play_wav_file(wav_path)

            except Exception as exc:
                print("\nERROR while generating or playing TTS audio:")
                print(exc)

            print(f"\nDialogue turn count: {pipeline.state.turn_count}\n")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    finally:
        print("\nStopping engagement recognizer...")
        engagement.stop()
        print("Stopped.")


if __name__ == "__main__":
    main()