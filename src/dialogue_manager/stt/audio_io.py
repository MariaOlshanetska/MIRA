from __future__ import annotations

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