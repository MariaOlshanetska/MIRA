from __future__ import annotations

import argparse
import random
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dialogue_manager.core.pipeline import DialoguePipeline
from dialogue_manager.engagement.realtime_subprocess import RealtimeEngagementSubprocessAnalyzer
from dialogue_manager.engagement.types import EngagementState
from dialogue_manager.llm.qwen_client import QwenLLMClient
from dialogue_manager.stt.audio_io import list_input_devices, wait_for_speech_then_record_until_silence
from dialogue_manager.stt.whisper_local import WhisperLocalSTT
from dialogue_manager.tts.formatter import build_tts_api_text
from dialogue_manager.core.turn import DialogueTurn, UserTurnInput
from dialogue_manager.output.annotation_parser import parse_annotated_response

from dialogue_manager.tts.audio_player import play_wav_file
from dialogue_manager.tts.expressive_client import ExpressiveTTSClient


DEFAULT_ENGAGEMENT_URL = "http://10.10.200.182/engagement_maria/engagement_maria"

DEFAULT_REPAIR_ANNOTATED_RESPONSES = [
    (
        "[emotion: neutral] *face: FACE_SOFT_SMILE* Let me pause there for a second. "
        "[silence: 0.3] *gesture: PALMS_UP_1* Is everything okay, or would you like me to rephrase?"
    ),
    (
        "[emotion: neutral] *face: FACE_CONFUSED_LOW* I might be going a bit too fast. "
        "[silence: 0.3] *gesture: PALMS_UP_1* Would you like me to slow down or ask that differently?"
    ),
    (
        "[emotion: neutral] *gesture: EMBLEM_WAIT_HOLDON_2* Let me stop there for a moment. "
        "[silence: 0.3] *face: FACE_SOFT_SMILE* Are you still with me?"
    ),
    (
        "[emotion: neutral] *face: FACE_CONFUSED_LOW* I feel like I am loosing you. "
        "[silence: 0.3] *gesture: PALMS_UP_1* Should I rephrase the question?"
    ),
    (
        "[emotion: neutral] *gesture: EMBLEM_WAIT_HOLDON_2* Let me pause the interview for a second. "
        "[silence: 0.3] *gesture: DEICTIC_YOU_1* Are you okay to continue?"
    ),
    (
        "[emotion: neutral] *face: FACE_SOFT_SMILE* I want this to feel like a conversation, not a lecture. "
        "[silence: 0.3] *gesture: PALMS_UP_1* Would you prefer a shorter question?"
    ),
]


@dataclass
class EngagementRepairRequest:
    requested: bool = False
    reason: str | None = None
    score: float | None = None
    drop: float | None = None
    observed_for_seconds: float = 0.0


def build_fallback_opening_annotated_response(candidate_profession: str) -> str:
    return (
        "[emotion: happiness] *face: FACE_SMILE_LOW* Hi, welcome. "
        "It is really nice to finally meet you. "
        "[silence: 0.3] *gesture: DEICTIC_ME_1* My name is Aera, and I will be guiding this first conversation today. "
        f"[silence: 0.3] *gesture: EXPLAIN_BEAT_1* This interview will be focused on your career as {candidate_profession}. "
        "[silence: 0.4] *gesture: DEICTIC_YOU_1* How are you doing today?"
    )

def build_opening_generation_instruction(candidate_profession: str) -> str:
    return (
        "[system_event: interview_start]\n"
        f"The candidate's profession or field is: {candidate_profession}.\n"
        "Generate Aera's first spoken turn of the interview.\n"
        "This is the first turn, before the candidate has spoken.\n"
        "Start warmly, introduce Aera and CCIA briefly, explain that this is a relaxed first conversation, "
        "and ask how the candidate is doing today.\n"
        "Do not ask about experience yet.\n"
        "Output only one annotated response."
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
    parser.add_argument("--speech-start-threshold", type=float, default=None)
    parser.add_argument("--speech-start-seconds", type=float, default=0.2)
    parser.add_argument("--pre-roll-seconds", type=float, default=0.3)
    parser.add_argument("--max-wait-seconds", type=float, default=None)

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

    # Engagement watchdog while Aera is speaking.
    # Current behaviour: because local playback is one blocking WAV, the watchdog
    # can detect disengagement during playback but only triggers a repair after
    # the current WAV returns.
    parser.add_argument("--disable-engagement-watchdog", action="store_true")
    parser.add_argument("--repair-threshold", type=float, default=0.30)
    parser.add_argument("--repair-drop-threshold", type=float, default=0.25)
    parser.add_argument("--repair-min-duration", type=float, default=1.2)
    parser.add_argument("--repair-cooldown", type=float, default=8.0)
    parser.add_argument("--engagement-monitor-interval", type=float, default=0.3)
    parser.add_argument(
        "--repair-response",
        type=str,
        default=None,
        help=(
            "Optional fixed annotated repair response. "
            "If omitted, the system randomly chooses one response from "
            "DEFAULT_REPAIR_ANNOTATED_RESPONSES."
        ),
    )

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
        print("Aera will start after setup. After Aera speaks, the microphone will listen automatically.")

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

    agent_speaking = False
    last_repair_time: float | None = None

    def play_agent_wav(
        wav_path: Path,
        *,
        monitor_engagement: bool = True,
        label: str = "agent_turn",
    ) -> EngagementRepairRequest:
        """
        Blocking local playback wrapper with an engagement watchdog.

        Current local behaviour:
        - The TTS API returns one complete WAV for the whole agent turn.
        - play_wav_file(...) is blocking and does not expose a clean cancellation
          hook here.
        - Therefore, the watchdog can detect that engagement dropped while Aera
          was speaking, but it can only trigger a repair immediately after the
          current WAV finishes.

        TODO for Unreal / chunked TTS integration:
        When TTS or Unreal playback is split into sentence-level or
        punctuation-level chunks, move this engagement check between chunks.
        Before each chunk, read engagement.get_latest_score(); if engagement has
        dropped below the repair threshold, skip the remaining chunks and send a
        short interaction-repair utterance instead. That will turn the current
        "repair after full WAV" behaviour into a real mid-turn change of plan.

        For Unreal integration, replace the local agent_speaking assignments
        with Jordi's event/callback layer, for example:
        on_agent_speech_start / on_agent_speech_end.
        """
        nonlocal agent_speaking, last_repair_time

        repair_request = EngagementRepairRequest()
        stop_monitor = threading.Event()
        monitor_thread: threading.Thread | None = None

        start_score = engagement.get_latest_score()
        baseline_score = 0.5 if start_score is None else start_score

        def monitor_engagement() -> None:
            below_since: float | None = None
            active_reason: str | None = None

            while not stop_monitor.wait(args.engagement_monitor_interval):
                current_score = engagement.get_latest_score()
                if current_score is None:
                    below_since = None
                    active_reason = None
                    continue

                drop = baseline_score - current_score
                absolute_low = current_score <= args.repair_threshold
                rapid_drop = drop >= args.repair_drop_threshold

                if absolute_low or rapid_drop:
                    now = time.time()
                    reason = "absolute_low" if absolute_low else "rapid_drop"

                    if below_since is None or active_reason != reason:
                        below_since = now
                        active_reason = reason

                    observed_for = now - below_since

                    cooldown_ok = (
                        last_repair_time is None
                        or now - last_repair_time >= args.repair_cooldown
                    )

                    if observed_for >= args.repair_min_duration and cooldown_ok:
                        repair_request.requested = True
                        repair_request.reason = reason
                        repair_request.score = current_score
                        repair_request.drop = drop
                        repair_request.observed_for_seconds = observed_for
                        print(
                            "\n[engagement-watchdog] Repair requested while Aera was speaking: "
                            f"reason={reason}, score={current_score:.3f}, "
                            f"drop={drop:.3f}, observed_for={observed_for:.1f}s",
                            flush=True,
                        )
                        stop_monitor.set()
                        return
                else:
                    below_since = None
                    active_reason = None

        agent_speaking = True
        print(f"agent_speaking=True ({label})")

        if monitor_engagement and not args.disable_engagement_watchdog:
            print(
                "[engagement-watchdog] Monitoring engagement during Aera speech "
                f"from baseline={baseline_score:.3f}",
                flush=True,
            )
            monitor_thread = threading.Thread(
                target=monitor_engagement,
                daemon=True,
            )
            monitor_thread.start()

        try:
            play_wav_file(wav_path)
        finally:
            stop_monitor.set()
            if monitor_thread is not None:
                monitor_thread.join(timeout=1.0)

            agent_speaking = False
            print(f"agent_speaking=False ({label})")

            if repair_request.requested:
                last_repair_time = time.time()

        return repair_request

    def synthesize_and_play_repair(repair_request: EngagementRepairRequest) -> None:
        """
        Play a short fixed interaction-repair utterance after engagement drops.

        This deliberately does not call Qwen: repair must be immediate, short,
        predictable, and safe. After the repair, the normal loop returns to
        listening mode so the candidate can answer.
        """
        if args.repair_response:
            repair_annotated_response = args.repair_response
        else:
            repair_annotated_response = random.choice(DEFAULT_REPAIR_ANNOTATED_RESPONSES)

        repair_output = parse_annotated_response(repair_annotated_response)

        print("\nEngagement repair response:")
        print_dialogue_output(repair_output)

        repair_output_path = (
            Path(tempfile.gettempdir())
            / "dialogue_manager"
            / "latest_engagement_repair_tts_output.wav"
        )

        try:
            repair_tts_text = build_tts_api_text(repair_output)
            print("\nRepair TTS API text:")
            print(repair_tts_text)

            repair_wav_path = tts_client.synthesize_to_file(
                text=repair_tts_text,
                output_path=repair_output_path,
            )

            print(f"Repair TTS WAV saved to: {repair_wav_path}")
            print("Playing engagement repair message...")

            play_agent_wav(
                repair_wav_path,
                monitor_engagement=False,
                label="engagement_repair",
            )

            pipeline.state.add_turn(
                DialogueTurn(
                    user_input=UserTurnInput(
                        user_text=(
                            "[system_event] Engagement dropped while Aera was speaking; "
                            "Aera made a brief interaction-repair move."
                        )
                    ),
                    engagement=EngagementState(
                        score=repair_request.score if repair_request.score is not None else 0.5,
                        summary="Engagement repair triggered during agent speech.",
                        metadata={
                            "source": "engagement_watchdog",
                            "reason": repair_request.reason,
                            "drop": repair_request.drop,
                            "observed_for_seconds": repair_request.observed_for_seconds,
                        },
                    ),
                    output=repair_output,
                    raw_llm_output=repair_annotated_response,
                    metadata={
                        "type": "engagement_repair",
                    },
                )
            )

        except Exception as exc:
            print("\nERROR while generating or playing engagement repair audio:")
            print(exc)

    print("\nVoice dialogue pipeline ready.")
    print("Aera will start the interview.")
    print("After Aera speaks, the microphone will listen automatically.")
    print("Press Ctrl+C to quit.")
    print("Speak in English after Aera's first question.\n")

    opening_generation_text = build_opening_generation_instruction(candidate_profession)
    opening_engagement_score = engagement.get_latest_score()

    try:
        print("\nGenerating Aera's opening turn with Qwen...")

        opening_output = pipeline.process_text_turn(
            opening_generation_text,
            engagement_score=opening_engagement_score,
        )

        opening_annotated_response = opening_output.annotated_response

    except Exception as exc:
        print("\nWARNING: Could not generate opening turn with Qwen.")
        print("Using fallback hard-coded opening.")
        print(exc)

        opening_annotated_response = build_fallback_opening_annotated_response(
            candidate_profession
        )
        opening_output = parse_annotated_response(opening_annotated_response)

        pipeline.state.add_turn(
            DialogueTurn(
                user_input=UserTurnInput(
                    user_text=(
                        "[system_event: interview_start_fallback]\n"
                        f"Candidate profession / field: {candidate_profession}\n"
                        "Aera used the fallback opening response."
                    )
                ),
                engagement=EngagementState(
                    score=opening_engagement_score if opening_engagement_score is not None else 0.5,
                    summary="Opening turn generated from fallback.",
                    metadata={
                        "source": "agent_opening_fallback",
                        "ready": opening_engagement_score is not None,
                    },
                ),
                output=opening_output,
                raw_llm_output=opening_annotated_response,
                metadata={
                    "type": "agent_opening",
                    "candidate_profession": candidate_profession,
                    "fallback": True,
                },
            )
        )

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

        play_agent_wav(wav_path, monitor_engagement=False, label="opening")

    except Exception as exc:
        agent_speaking = False
        print("\nERROR while generating or playing opening TTS audio:")
        print(exc)

    print("\nAera finished speaking. Listening is now automatic.")

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
            score_text = "unknown" if current_score is None else f"{current_score:.3f}"
            print(f"\nCurrent engagement={score_text}. Waiting for candidate speech...")

            if agent_speaking:
                # In this local TTS version, play_agent_wav is blocking, so this should
                # normally never happen. The guard is useful for future Unreal integration.
                print("Agent is still speaking; microphone is not armed yet.")
                continue

            with tempfile.TemporaryDirectory() as temp_dir:
                audio_path = Path(temp_dir) / "user_turn.wav"

                speech_detected = wait_for_speech_then_record_until_silence(
                    output_path=audio_path,
                    sample_rate=args.sample_rate,
                    device_index=args.device_index,
                    speech_start_threshold=(
                        args.speech_start_threshold
                        if args.speech_start_threshold is not None
                        else args.silence_threshold
                    ),
                    speech_start_seconds=args.speech_start_seconds,
                    silence_seconds=args.silence_seconds,
                    silence_threshold=args.silence_threshold,
                    min_record_seconds=args.min_record_seconds,
                    max_record_seconds=args.duration,
                    max_wait_seconds=args.max_wait_seconds,
                    pre_roll_seconds=args.pre_roll_seconds,
                )

                if not speech_detected:
                    continue

                print("\nTranscribing with Whisper...")
                transcription = stt.transcribe_file(audio_path)

            print("\n" + "-" * 60)
            print("WHISPER TRANSCRIPTION")
            print("-" * 60)
            print(transcription.text if transcription.text else "[EMPTY]")
            print("-" * 60)

            if not transcription.text:
                print("No speech detected by Whisper. Listening again.\n")
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

                repair_request = play_agent_wav(
                    wav_path,
                    monitor_engagement=True,
                    label="agent_turn",
                )

                if repair_request.requested:
                    synthesize_and_play_repair(repair_request)

            except Exception as exc:
                agent_speaking = False
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
