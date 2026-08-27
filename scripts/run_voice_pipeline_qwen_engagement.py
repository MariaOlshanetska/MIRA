from __future__ import annotations

import argparse
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dialogue_manager.core.pipeline import DialoguePipeline
from dialogue_manager.engagement.realtime_subprocess import RealtimeEngagementSubprocessAnalyzer
from dialogue_manager.engagement.types import EngagementState
from dialogue_manager.llm.qwen_client import QwenLLMClient
from dialogue_manager.stt.audio_io import (
    RecordingResult,
    list_input_devices,
    wait_for_speech_then_record_until_silence,
)
from dialogue_manager.stt.whisper_local import WhisperLocalSTT
from dialogue_manager.tts.formatter import build_tts_api_text
from dialogue_manager.core.turn import DialogueTurn, UserTurnInput
from dialogue_manager.output.annotation_parser import parse_annotated_response

from dialogue_manager.tts.audio_player import play_wav_file, play_wav_file_interruptible
from dialogue_manager.tts.expressive_client import ExpressiveTTSClient


DEFAULT_ENGAGEMENT_URL = "http://10.10.200.182/engagement_maria/engagement_maria"

# =============================================================================
# 3-STRIKE REPAIR SYSTEM
# =============================================================================
# When engagement drops below the repair threshold while Aera is speaking,
# the system triggers an escalating repair:
#
# Strike 1: Soft check-in — "Is everything okay?"
# Strike 2: Firmer notice — "I feel like you are not very present right now."
# Strike 3: End conversation gracefully and close the session.
#
# After each strike, a cooldown period (default 15s) must pass before the
# next strike can be triggered.
# =============================================================================

STRIKE_RESPONSES = [
    # Strike 1 — Soft, friendly check-in
    # (Preceded by a thinking burst after the mid-speech interruption)
    (
        "[silence: 0.4] [burst: sight_1] [silence: 0.3] "
        "[emotion: neutral] *face: FACE_SOFT_SMILE* *gesture: EMBLEM_WAIT_HOLDON_2* "
        "Let me pause for a second. [silence: 0.3] "
        "*gesture: DEICTIC_YOU_1* Is everything okay? [silence: 0.2] "
        "*gesture: PALMS_UP_1* We can slow down if you need."
    ),
    # Strike 2 — Firmer, still professional
    (
        "[silence: 0.4] [burst: sight_2] [silence: 0.3] "
        "[emotion: neutral] *face: FACE_CONFUSED_LOW* *gesture: EMBLEM_WAIT_HOLDON_2* "
        "I am going to stop here for a moment. [silence: 0.3] "
        "*face: FACE_FRUSTRATED* I feel like you are not very present right now. [silence: 0.3] "
        "*gesture: PALMS_UP_1* Should we continue, or would you prefer to take a break?"
    ),
    # Strike 3 — Graceful end of conversation
    (
        "[silence: 0.4] [burst: sight_3] [silence: 0.3] "
        "[emotion: neutral] *face: FACE_SOFT_SMILE* *gesture: EMBLEM_WAIT_HOLDON_2* "
        "Okay, I think this is a good moment to wrap up. [silence: 0.3] "
        "*gesture: EXPLAIN_BEAT_1* It seems like today might not be the best time for this. "
        "[silence: 0.3] *face: FACE_SMILE_LOW* *gesture: QUICK_NOD_1* "
        "No worries at all, we can always pick this up another day. [silence: 0.2] "
        "*gesture: DEICTIC_YOU_1* Take care."
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
        "[emotion: happiness] *face: FACE_SMILE_LOW* Hey, welcome! "
        "*gesture: DEICTIC_ME_1* I am Aera, part of the team here at CCIA. "
        f"[silence: 0.3] *gesture: EXPLAIN_BEAT_1* We are going to keep this super relaxed, "
        f"just a short chat about your work in {candidate_profession}. "
        "[silence: 0.3] *face: FACE_SOFT_SMILE* *gesture: DEICTIC_YOU_1* "
        "So, how are you doing today?"
    )


def build_opening_generation_instruction(candidate_profession: str) -> str:
    return (
        "[system_event: interview_start]\n"
        f"The candidate's profession or field is: {candidate_profession}.\n"
        "Generate Aera's first spoken turn of the interview.\n"
        "This is the first turn, before the candidate has spoken.\n"
        "Keep it short: two or three spoken sentences maximum.\n"
        "Include a warm greeting, Aera's name, CCIA, and a light check-in.\n"
        "You may briefly mention the candidate's field, but do not ask about experience yet.\n"
        "Use at least one facial expression and one gesture.\n"
        "Do not say 'no formal checklists', 'scripted assessment', or similar meta-comments.\n"
        "Do not over-explain the interview process.\n"
        "Do not copy examples from the system prompt word-for-word.\n"
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
    parser = argparse.ArgumentParser(
        description="Voice dialogue pipeline with Qwen LLM and engagement monitoring.",
    )

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
        default="scripts/realtime_engagement_demo.py",
    )
    parser.add_argument("--engagement-url", type=str, default=DEFAULT_ENGAGEMENT_URL)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--engagement-audio-device", default=None)
    parser.add_argument("--calib", type=float, default=5.0)
    parser.add_argument("--engagement-fps", type=float, default=3.0)
    parser.add_argument("--fusion-fps", type=float, default=3.0)
    parser.add_argument("--show-engagement-window", action="store_true")

    # Engagement watchdog (3-strike system)
    parser.add_argument("--disable-engagement-watchdog", action="store_true")
    parser.add_argument("--repair-threshold", type=float, default=0.20,
                        help="Engagement score below which a strike is triggered")
    parser.add_argument("--repair-drop-threshold", type=float, default=0.25,
                        help="Rapid drop from baseline that also triggers a strike")
    parser.add_argument("--repair-min-duration", type=float, default=1.2,
                        help="Seconds engagement must stay low before triggering strike")
    parser.add_argument("--repair-cooldown", type=float, default=15.0,
                        help="Minimum seconds between consecutive strikes")
    parser.add_argument("--engagement-monitor-interval", type=float, default=0.3)

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
    pipeline.state.variables["candidate_profession"] = candidate_profession
    pipeline.state.variables["opening_delivered"] = False

    # =========================================================================
    # 3-STRIKE STATE
    # =========================================================================
    agent_speaking = False
    strike_count = 0          # 0 = no strikes yet, max = 3
    last_strike_time: float | None = None
    session_ended = False     # Set to True after strike 3 → exit main loop

    def play_agent_wav(
        wav_path: Path,
        *,
        monitor_engagement: bool = True,
        label: str = "agent_turn",
    ) -> EngagementRepairRequest:
        """
        Interruptible playback wrapper with engagement watchdog.

        Uses play_wav_file_interruptible() so that when the watchdog detects
        an engagement drop, it can STOP playback mid-audio. Aera literally
        stops talking, pauses briefly, and then the strike fires.
        """
        nonlocal agent_speaking, last_strike_time

        repair_request = EngagementRepairRequest()
        stop_playback = threading.Event()   # Set this to halt audio immediately
        stop_monitor = threading.Event()
        monitor_thread: threading.Thread | None = None

        start_score = engagement.get_latest_score()
        baseline_score = 0.5 if start_score is None else start_score

        def _monitor_engagement_loop() -> None:
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
                        last_strike_time is None
                        or now - last_strike_time >= args.repair_cooldown
                    )

                    if observed_for >= args.repair_min_duration and cooldown_ok:
                        repair_request.requested = True
                        repair_request.reason = reason
                        repair_request.score = current_score
                        repair_request.drop = drop
                        repair_request.observed_for_seconds = observed_for
                        print(
                            f"\n[engagement-watchdog] Strike trigger — INTERRUPTING playback! "
                            f"reason={reason}, score={current_score:.3f}, "
                            f"drop={drop:.3f}, observed_for={observed_for:.1f}s",
                            flush=True,
                        )
                        # Stop playback immediately
                        stop_playback.set()
                        stop_monitor.set()
                        return
                else:
                    below_since = None
                    active_reason = None

        agent_speaking = True
        print(f"agent_speaking=True ({label})")

        if monitor_engagement and not args.disable_engagement_watchdog:
            print(
                f"[engagement-watchdog] Monitoring during Aera speech "
                f"(baseline={baseline_score:.3f}, strikes={strike_count}/3)",
                flush=True,
            )
            monitor_thread = threading.Thread(
                target=_monitor_engagement_loop,
                daemon=True,
            )
            monitor_thread.start()

        try:
            if monitor_engagement and not args.disable_engagement_watchdog:
                # Interruptible playback — stops if stop_playback is set
                completed = play_wav_file_interruptible(wav_path, stop_playback)
                if not completed:
                    print(f"[playback] Audio interrupted mid-speech ({label})")
            else:
                # Non-interruptible for strikes/opening (no watchdog)
                play_wav_file(wav_path)
        finally:
            stop_monitor.set()
            if monitor_thread is not None:
                monitor_thread.join(timeout=1.0)

            agent_speaking = False
            print(f"agent_speaking=False ({label})")

            if repair_request.requested:
                last_strike_time = time.time()

        return repair_request

    def execute_strike(repair_request: EngagementRepairRequest) -> None:
        """
        Execute the next strike in the escalating repair system.

        Strike 1: Soft check-in
        Strike 2: Firmer warning
        Strike 3: End conversation and set session_ended = True
        """
        nonlocal strike_count, session_ended

        strike_count += 1
        strike_index = min(strike_count - 1, len(STRIKE_RESPONSES) - 1)
        strike_annotated_response = STRIKE_RESPONSES[strike_index]

        print(f"\n{'='*60}")
        print(f"STRIKE {strike_count} / 3")
        print(f"{'='*60}")
        print(f"Reason: {repair_request.reason}")
        print(f"Score at trigger: {repair_request.score:.3f}" if repair_request.score else "")
        print(f"{'='*60}")

        strike_output = parse_annotated_response(strike_annotated_response)
        print_dialogue_output(strike_output)

        strike_wav_path = (
            Path(tempfile.gettempdir())
            / "dialogue_manager"
            / f"strike_{strike_count}_tts_output.wav"
        )

        try:
            strike_tts_text = build_tts_api_text(strike_output)
            print(f"\nStrike {strike_count} TTS text:")
            print(strike_tts_text)

            wav_path = tts_client.synthesize_to_file(
                text=strike_tts_text,
                output_path=strike_wav_path,
            )

            print(f"Strike WAV saved to: {wav_path}")
            print(f"Playing strike {strike_count} message...")

            # Do not monitor engagement during strike playback
            play_agent_wav(
                wav_path,
                monitor_engagement=False,
                label=f"strike_{strike_count}",
            )

            # Record in dialogue history
            pipeline.state.add_turn(
                DialogueTurn(
                    user_input=UserTurnInput(
                        user_text=(
                            f"[system_event] Engagement repair strike {strike_count}/3 triggered. "
                            f"Reason: {repair_request.reason}. "
                            f"Score: {repair_request.score if repair_request.score is not None else 'unknown'}."
                        )
                    ),
                    engagement=EngagementState(
                        score=repair_request.score if repair_request.score is not None else 0.5,
                        summary=f"Strike {strike_count}/3 triggered.",
                        metadata={
                            "source": "engagement_watchdog",
                            "strike": strike_count,
                            "reason": repair_request.reason,
                            "drop": repair_request.drop,
                            "observed_for_seconds": repair_request.observed_for_seconds,
                        },
                    ),
                    output=strike_output,
                    raw_llm_output=strike_annotated_response,
                    metadata={
                        "type": "engagement_strike",
                        "strike_number": strike_count,
                    },
                )
            )

        except Exception as exc:
            print(f"\nERROR while generating or playing strike {strike_count} audio:")
            print(exc)

        # After strike 3, end the session
        if strike_count >= 3:
            print("\n" + "=" * 60)
            print("SESSION ENDED — 3 strikes reached.")
            print("=" * 60)
            session_ended = True

    # =========================================================================
    # INTERVIEW START
    # =========================================================================

    print("\nVoice dialogue pipeline ready.")
    print("Aera will start the interview.")
    print("After Aera speaks, the microphone will listen automatically.")
    print("3-strike system active: engagement drops trigger escalating repairs.")
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

    pipeline.state.variables["opening_delivered"] = True

    print("\nAera finished speaking. Listening is now automatic.")

    engagement_warning_printed = False

    # =========================================================================
    # MAIN CONVERSATION LOOP
    # =========================================================================
    try:
        while not session_ended:
            if not engagement.is_running() and not engagement_warning_printed:
                print("\nWARNING: Engagement recognizer is no longer running.")
                print("This probably happened because the OpenCV window was closed or q was pressed.")
                print("The dialogue manager will keep using the last available engagement value.")
                print("Restart the script if you need live engagement again.\n")
                engagement_warning_printed = True

            current_score = engagement.get_latest_score()
            score_text = "unknown" if current_score is None else f"{current_score:.3f}"
            print(f"\nCurrent engagement={score_text} | Strikes={strike_count}/3 | Waiting for candidate speech...")

            if agent_speaking:
                print("Agent is still speaking; microphone is not armed yet.")
                continue

            # =================================================================
            # CONTINUOUS WATCHDOG DURING THE CANDIDATE TURN
            # Engagement is monitored while the microphone is armed and while
            # the candidate is speaking. If the score falls to/below the repair
            # threshold (default 0.20) and the cooldown has elapsed, the
            # recording is aborted mid-turn so Aera can interrupt the candidate
            # immediately instead of waiting for them to finish.
            # =================================================================
            def _should_interrupt_candidate() -> bool:
                if args.disable_engagement_watchdog or strike_count >= 3:
                    return False
                score = engagement.get_latest_score()
                if score is None or score > args.repair_threshold:
                    return False
                cooldown_ok = (
                    last_strike_time is None
                    or time.time() - last_strike_time >= args.repair_cooldown
                )
                return cooldown_ok

            with tempfile.TemporaryDirectory() as temp_dir:
                audio_path = Path(temp_dir) / "user_turn.wav"

                recording_result = wait_for_speech_then_record_until_silence(
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
                    abort_check=_should_interrupt_candidate,
                )

                # The watchdog interrupted the candidate mid-turn: fire a strike
                # instead of transcribing an abandoned recording.
                if recording_result == RecordingResult.ABORTED:
                    interrupt_score = engagement.get_latest_score()
                    print(
                        f"\n[engagement-check] Engagement="
                        f"{interrupt_score if interrupt_score is None else round(interrupt_score, 3)} "
                        f"(at or below {args.repair_threshold}) during candidate turn — "
                        f"interrupting the candidate and triggering strike!",
                        flush=True,
                    )
                    interrupt_request = EngagementRepairRequest(
                        requested=True,
                        reason="low_during_candidate_turn",
                        score=interrupt_score,
                        drop=None,
                        observed_for_seconds=0.0,
                    )
                    last_strike_time = time.time()
                    execute_strike(interrupt_request)
                    if session_ended:
                        break
                    # After the strike, go back to listening (skip Qwen this turn)
                    continue

                if recording_result != RecordingResult.RECORDED:
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

                if repair_request.requested and strike_count < 3:
                    execute_strike(repair_request)

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

        if session_ended:
            print("\nSession ended after 3 engagement strikes.")
            print(f"Total dialogue turns: {pipeline.state.turn_count}")
        
        print("Goodbye.")


if __name__ == "__main__":
    main()
