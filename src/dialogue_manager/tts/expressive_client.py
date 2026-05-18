from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv


class ExpressiveTTSClient:
    """
    Client for the lab expressive TTS API.
    """

    def __init__(
        self,
        url: str | None = None,
        language: str | None = None,
        temperature: float | None = None,
        speed: float | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        load_dotenv()

        self.url = url or os.getenv("TTS_API_URL") or "http://10.10.200.182/tts/expressive_read"
        self.language = language or os.getenv("TTS_LANGUAGE") or "en"
        self.temperature = float(temperature or os.getenv("TTS_TEMPERATURE") or 0.4)
        self.speed = float(speed or os.getenv("TTS_SPEED") or 1.0)
        self.timeout_seconds = timeout_seconds

    def synthesize_to_file(
        self,
        text: str,
        output_path: Path,
    ) -> Path:
        payload = {
            "text": text,
            "temperature": self.temperature,
            "language": self.language,
            "speed": self.speed,
        }

        response = requests.post(
            self.url,
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.content)

        return output_path