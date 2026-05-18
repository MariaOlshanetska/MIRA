from __future__ import annotations

import tempfile
from pathlib import Path

from dialogue_manager.tts.audio_player import play_wav_file
from dialogue_manager.tts.expressive_client import ExpressiveTTSClient


def main() -> None:
    tts = ExpressiveTTSClient()

    text = (
        "text = "<burst(yawn_1)> <silence(0.5)> <burst(thinking_1)> <silence(0.5)> <burst(ouch_1)>""
    )

    output_path = Path(tempfile.gettempdir()) / "dialogue_manager" / "tts_test.wav"

    print(f"Sending text to TTS API: {tts.url}")
    print(f"Text: {text}")

    wav_path = tts.synthesize_to_file(
        text=text,
        output_path=output_path,
    )

    print(f"Saved audio to: {wav_path}")
    print("Playing audio...")

    play_wav_file(wav_path)

    print("Done.")


if __name__ == "__main__":
    main()