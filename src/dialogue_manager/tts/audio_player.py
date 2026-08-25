from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf


def play_wav_file(path: Path) -> None:
    """
    Play a WAV file locally (blocking, non-interruptible).
    Kept for backward compatibility.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"WAV file not found: {path}")

    data, samplerate = sf.read(path, dtype="float32")
    sd.play(data, samplerate)
    sd.wait()


def play_wav_file_interruptible(
    path: Path,
    stop_event: threading.Event,
    check_interval: float = 0.05,
) -> bool:
    """
    Play a WAV file with the ability to stop mid-playback.

    Args:
        path: Path to the WAV file.
        stop_event: A threading.Event. If set externally, playback stops immediately.
        check_interval: How often (seconds) to check the stop_event during playback.

    Returns:
        True if playback completed normally.
        False if playback was interrupted (stop_event was set).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"WAV file not found: {path}")

    data, samplerate = sf.read(path, dtype="float32")
    duration = len(data) / samplerate

    sd.play(data, samplerate)

    elapsed = 0.0
    while elapsed < duration:
        if stop_event.is_set():
            sd.stop()
            return False
        time.sleep(check_interval)
        elapsed += check_interval

    sd.wait()
    return True
