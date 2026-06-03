from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from dialogue_manager.engagement.base import EngagementAnalyzer


class RealtimeEngagementSubprocessAnalyzer(EngagementAnalyzer):
    """
    Engagement analyzer that runs the existing realtime engagement recognizer
    as a background subprocess and keeps the latest engagement score.

    The external script must print one JSON row per update when called with:
        --print-format json
    This class deliberately exposes only the latest numeric score to the
    dialogue manager. Dialogue policy belongs in the prompt.
    """

    def __init__(
        self,
        script_path: Path,
        url: str,
        camera: int = 0,
        audio_device: int | str | None = None,
        calib_seconds: float = 5.0,
        fps: float = 3.0,
        fusion_fps: float = 3.0,
        no_window: bool = True,
        overwrite_log: bool = True,
    ) -> None:
        self.script_path = Path(script_path)
        self.url = url
        self.camera = camera
        self.audio_device = audio_device
        self.calib_seconds = calib_seconds
        self.fps = fps
        self.fusion_fps = fusion_fps
        self.no_window = no_window

        log_dir = Path(tempfile.gettempdir()) / "dialogue_manager"
        log_dir.mkdir(parents=True, exist_ok=True)

        if overwrite_log:
            self.log_path = log_dir / "realtime_engagement_latest.csv"
        else:
            self.log_path = log_dir / f"realtime_engagement_{int(time.time())}.csv"

        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

        self.latest_row: dict[str, Any] | None = None
        self.latest_update_time: float | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return

        self.stop_event.clear()

        if not self.script_path.exists():
            raise FileNotFoundError(f"Engagement script not found: {self.script_path}")
        cmd = [
            sys.executable,
            str(self.script_path),
            "--url",
            self.url,
            "--camera",
            str(self.camera),
            "--fps",
            str(self.fps),
            "--fusion-fps",
            str(self.fusion_fps),
            "--calib",
            str(self.calib_seconds),
            "--out",
            str(self.log_path),
            "--print-format",
            "json",
        ]

        if self.no_window:
            cmd.append("--no-window")

        if self.audio_device is not None:
            cmd.extend(["--audio-device", str(self.audio_device)])

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        self.reader_thread = threading.Thread(
            target=self._read_stdout_loop,
            daemon=True,
        )
        self.reader_thread.start()

    def stop(self) -> None:
        self.stop_event.set()

        if self.process is None:
            return

        if self.process.poll() is None:
            self.process.terminate()

            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

        self.process = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def get_process_status(self) -> str:
        if self.process is None:
            return "not_started"

        return_code = self.process.poll()

        if return_code is None:
            return "running"

        return f"stopped_with_code_{return_code}"

    def _read_stdout_loop(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None

        for line in self.process.stdout:
            if self.stop_event.is_set():
                break

            line = line.strip()

            if not line:
                continue

            # The engagement script also prints normal text.
            # We only keep JSON rows.
            if not line.startswith("{"):
                print(f"[engagement] {line}", flush=True)
                continue

            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                print(f"[engagement non-json] {line}", flush=True)
                continue

            with self.lock:
                self.latest_row = row
                self.latest_update_time = time.time()
                self.last_error = None

    def wait_until_ready(self, timeout_seconds: float = 15.0) -> bool:
        """
        Wait until at least one post-calibration engagement row has arrived.
        """

        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            with self.lock:
                if self.latest_row is not None:
                    return True

            if self.process is not None and self.process.poll() is not None:
                self.last_error = f"Engagement process exited with code {self.process.returncode}"
                return False

            time.sleep(0.1)

        return False

    def get_latest_score(self) -> float | None:
        """
        Return only the latest engagement score produced by the realtime script.
        """
        with self.lock:
            if not self.latest_row:
                return None

            value = self.latest_row.get("engagement")

        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, score))