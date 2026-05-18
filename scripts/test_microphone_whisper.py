from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel


SAMPLE_RATE = 16000
MODEL_NAME = "medium.en"
COMPUTE_TYPE = "int8"

# Pon aquí tu micro si ya sabes el índice.
# Ejemplo: DEVICE_INDEX = 2
DEVICE_INDEX = None

# Whisper no es streaming real, así que usamos una ventana móvil.
WINDOW_SECONDS = 8.0          # cuántos segundos recientes mira Whisper
UPDATE_EVERY_SECONDS = 1.5    # cada cuánto actualiza la transcripción
MIN_AUDIO_SECONDS = 2.0       # espera mínima antes de transcribir
SILENCE_THRESHOLD = 0.01


audio_buffer = deque(maxlen=int(SAMPLE_RATE * WINDOW_SECONDS))
audio_lock = threading.Lock()


def audio_callback(indata, frames, time_info, status) -> None:
    if status:
        print(f"\nAudio status: {status}", flush=True)

    samples = indata[:, 0].copy()

    with audio_lock:
        audio_buffer.extend(samples)


def get_audio_snapshot() -> np.ndarray:
    with audio_lock:
        return np.array(audio_buffer, dtype=np.float32)


def print_live_line(text: str, previous_length: int) -> int:
    line = f"LIVE: {text}"
    padding = " " * max(0, previous_length - len(line))
    print("\r" + line + padding, end="", flush=True)
    return len(line)


def main() -> None:
    print(f"Loading Whisper model: {MODEL_NAME}")
    model = WhisperModel(
        MODEL_NAME,
        device="cpu",
        compute_type=COMPUTE_TYPE,
    )
    print("Model loaded.")
    print("Speak in English. Press Ctrl+C to stop.\n")

    last_printed_text = ""
    last_line_length = 0

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            device=DEVICE_INDEX,
            callback=audio_callback,
        ):
            while True:
                time.sleep(UPDATE_EVERY_SECONDS)

                audio = get_audio_snapshot()

                if len(audio) < int(SAMPLE_RATE * MIN_AUDIO_SECONDS):
                    continue

                max_amplitude = float(np.max(np.abs(audio)))

                if max_amplitude < SILENCE_THRESHOLD:
                    if last_printed_text != "":
                        last_printed_text = ""
                        last_line_length = print_live_line("", last_line_length)
                    continue

                segments, _ = model.transcribe(
                    audio,
                    language="en",
                    task="transcribe",
                    beam_size=1,
                    vad_filter=True,
                    condition_on_previous_text=False,
                )

                text = " ".join(segment.text.strip() for segment in segments).strip()

                if text and text != last_printed_text:
                    last_printed_text = text
                    last_line_length = print_live_line(text, last_line_length)

    except KeyboardInterrupt:
        print("\n\nStopped.")


if __name__ == "__main__":
    main()