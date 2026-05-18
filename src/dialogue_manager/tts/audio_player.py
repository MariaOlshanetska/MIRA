from __future__ import annotations

import sys
from pathlib import Path


def play_wav_file(path: Path) -> None:
    """
    Play a WAV file locally.

    On Windows, this uses the built-in winsound module.
    """

    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"WAV file not found: {path}")

    if sys.platform == "win32":
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return

    raise RuntimeError(
        "Automatic WAV playback is only implemented for Windows right now. "
        f"Audio was saved to: {path}"
    )