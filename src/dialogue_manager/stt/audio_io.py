from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf


def list_input_devices() -> None:
    """
    Print available input microphones.
    """

    print("\nAvailable input devices:\n")

    default_input = sd.default.device[0]

    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            marker = "  <-- DEFAULT" if index == default_input else ""
            print(
                f"{index}: {device['name']} "
                f"| channels={device['max_input_channels']} "
                f"| default_sr={device['default_samplerate']}"
                f"{marker}"
            )


def record_microphone_to_file(
    output_path: Path,
    duration_seconds: float = 5.0,
    sample_rate: int = 16000,
    device_index: int | None = None,
) -> None:
    """
    Record a fixed-duration microphone sample to a WAV file.
    """

    print(f"\nRecording {duration_seconds:.1f} seconds...")
    print("Speak in English now.\n", flush=True)

    audio = sd.rec(
        int(duration_seconds * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        device=device_index,
    )

    sd.wait()

    max_amplitude = float(np.max(np.abs(audio))) if audio.size else 0.0
    print(f"Max microphone amplitude: {max_amplitude:.4f}")

    if max_amplitude < 0.005:
        print(
            "WARNING: very low microphone signal. "
            "Check the selected input device or input volume."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, audio, sample_rate)

    print(f"Audio saved to: {output_path}")


def record_microphone_until_silence(
    output_path: Path,
    sample_rate: int = 16000,
    device_index: int | None = None,
    silence_seconds: float = 1.2,
    silence_threshold: float = 0.015,
    min_record_seconds: float = 1.0,
    max_record_seconds: float = 30.0,
    block_seconds: float = 0.1,
) -> None:
    """
    Record microphone audio until there is continuous silence.

    The recording stops when:
    - at least min_record_seconds have been recorded, and
    - the signal stays below silence_threshold for silence_seconds.

    A max_record_seconds limit is used as a safety fallback.
    """

    import time

    print("\nRecording until silence...")
    print(f"Stop condition: silence longer than {silence_seconds:.1f}s")
    print("Speak in English now.\n", flush=True)

    block_samples = int(sample_rate * block_seconds)
    max_blocks = int(max_record_seconds / block_seconds)
    silence_blocks_needed = int(silence_seconds / block_seconds)
    min_blocks_needed = int(min_record_seconds / block_seconds)

    recorded_blocks: list[np.ndarray] = []
    silent_blocks = 0

    start_time = time.time()

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        device=device_index,
        blocksize=block_samples,
    ) as stream:
        for block_index in range(max_blocks):
            block, overflowed = stream.read(block_samples)

            if overflowed:
                print("WARNING: audio input overflowed.", flush=True)

            block = block.copy()
            recorded_blocks.append(block)

            rms = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
            elapsed = time.time() - start_time

            if rms < silence_threshold:
                silent_blocks += 1
            else:
                silent_blocks = 0

            has_minimum_audio = block_index >= min_blocks_needed
            has_enough_silence = silent_blocks >= silence_blocks_needed

            if has_minimum_audio and has_enough_silence:
                print(
                    f"Detected {silence_seconds:.1f}s of silence. Stopping recording.",
                    flush=True,
                )
                break

    if not recorded_blocks:
        audio = np.zeros((1, 1), dtype=np.float32)
    else:
        audio = np.concatenate(recorded_blocks, axis=0)

    max_amplitude = float(np.max(np.abs(audio))) if audio.size else 0.0
    print(f"Recorded duration: {len(audio) / sample_rate:.2f}s")
    print(f"Max microphone amplitude: {max_amplitude:.4f}")

    if max_amplitude < 0.005:
        print(
            "WARNING: very low microphone signal. "
            "Check the selected input device or input volume."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, audio, sample_rate)

    print(f"Audio saved to: {output_path}")


def wait_for_speech_then_record_until_silence(
    output_path: Path,
    sample_rate: int = 16000,
    device_index: int | None = None,
    speech_start_threshold: float = 0.015,
    speech_start_seconds: float = 0.2,
    silence_seconds: float = 1.2,
    silence_threshold: float = 0.015,
    min_record_seconds: float = 1.0,
    max_record_seconds: float = 30.0,
    max_wait_seconds: float | None = None,
    pre_roll_seconds: float = 0.3,
    block_seconds: float = 0.1,
) -> bool:
    """
    Wait until speech is detected, then record until continuous silence.

    This is intended for turn-taking dialogue:
    - while the agent is speaking, do not call this function;
    - once the agent has finished, call it to arm the microphone;
    - it ignores initial silence and starts saving audio only after speech begins;
    - it returns True if speech was recorded, False if max_wait_seconds elapsed.

    The thresholds are simple RMS thresholds, not a semantic VAD. Whisper still
    receives the final audio and applies its own VAD during transcription.
    """

    import time

    block_samples = int(sample_rate * block_seconds)
    max_record_blocks = int(max_record_seconds / block_seconds)
    speech_blocks_needed = max(1, int(speech_start_seconds / block_seconds))
    silence_blocks_needed = max(1, int(silence_seconds / block_seconds))
    min_blocks_needed = max(1, int(min_record_seconds / block_seconds))
    pre_roll_blocks = max(0, int(pre_roll_seconds / block_seconds))

    pre_roll: deque[np.ndarray] = deque(maxlen=pre_roll_blocks)
    recorded_blocks: list[np.ndarray] = []

    speech_blocks = 0
    silent_blocks = 0
    recording_started = False
    wait_started_at = time.time()

    print("\nListening for candidate speech...")
    print(
        f"Start condition: RMS >= {speech_start_threshold:.4f} "
        f"for {speech_start_seconds:.1f}s"
    )
    print(f"Stop condition: silence longer than {silence_seconds:.1f}s")
    print("Speak in English when you are ready. Press Ctrl+C to quit.\n", flush=True)

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        device=device_index,
        blocksize=block_samples,
    ) as stream:
        while True:
            if (
                not recording_started
                and max_wait_seconds is not None
                and time.time() - wait_started_at >= max_wait_seconds
            ):
                print(
                    f"No speech detected after {max_wait_seconds:.1f}s. Skipping turn.",
                    flush=True,
                )
                return False

            block, overflowed = stream.read(block_samples)

            if overflowed:
                print("WARNING: audio input overflowed.", flush=True)

            block = block.copy()
            rms = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0

            if not recording_started:
                if pre_roll_blocks > 0:
                    pre_roll.append(block)

                if rms >= speech_start_threshold:
                    speech_blocks += 1
                else:
                    speech_blocks = 0

                if speech_blocks >= speech_blocks_needed:
                    recording_started = True
                    recorded_blocks.extend(pre_roll)
                    silent_blocks = 0
                    print("Speech detected. Recording user turn...", flush=True)

                continue

            recorded_blocks.append(block)

            if rms < silence_threshold:
                silent_blocks += 1
            else:
                silent_blocks = 0

            has_minimum_audio = len(recorded_blocks) >= min_blocks_needed
            has_enough_silence = silent_blocks >= silence_blocks_needed
            reached_max_recording = len(recorded_blocks) >= max_record_blocks

            if has_minimum_audio and has_enough_silence:
                print(
                    f"Detected {silence_seconds:.1f}s of silence. Stopping recording.",
                    flush=True,
                )
                break

            if reached_max_recording:
                print(
                    f"Reached max recording duration of {max_record_seconds:.1f}s. Stopping recording.",
                    flush=True,
                )
                break

    if not recorded_blocks:
        return False

    audio = np.concatenate(recorded_blocks, axis=0)

    max_amplitude = float(np.max(np.abs(audio))) if audio.size else 0.0
    print(f"Recorded duration: {len(audio) / sample_rate:.2f}s")
    print(f"Max microphone amplitude: {max_amplitude:.4f}")

    if max_amplitude < 0.005:
        print(
            "WARNING: very low microphone signal. "
            "Check the selected input device or input volume."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, audio, sample_rate)

    print(f"Audio saved to: {output_path}")
    return True
