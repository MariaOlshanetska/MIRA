from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from dialogue_manager.core.state import DialogueState
from dialogue_manager.core.turn import UserTurnInput
from dialogue_manager.engagement.base import EngagementAnalyzer
from dialogue_manager.engagement.types import EngagementSignal, EngagementState


class RealtimeEngagementSubprocessAnalyzer(EngagementAnalyzer):
    """
    Engagement analyzer that runs the existing realtime engagement recognizer
    as a background subprocess and keeps the latest engagement score.

    The external script must print one JSON row per update when called with:
        --print-format json
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

    def snapshot(self) -> EngagementState:
        dummy_input = UserTurnInput(user_text="[snapshot]")
        dummy_state = DialogueState()
        return self.analyze(dummy_input, dummy_state)

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
        with self.lock:
            if not self.latest_row:
                return None

            value = self.latest_row.get("engagement")

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def analyze(
        self,
        user_input: UserTurnInput,
        state: DialogueState,
    ) -> EngagementState:
        with self.lock:
            row = dict(self.latest_row) if self.latest_row is not None else None
            latest_update_time = self.latest_update_time

        if row is None:
            return EngagementState(
                level="medium",
                score=0.5,
                signals=[
                    EngagementSignal(
                        name="engagement_available",
                        value=False,
                        confidence=1.0,
                    )
                ],
                summary=(
                    "No realtime engagement value is available yet. "
                    "Use a neutral interviewing strategy."
                ),
                metadata={
                    "source": "realtime_subprocess",
                    "ready": False,
                    "log_path": str(self.log_path),
                },
            )

        score = float(row.get("engagement", 0.5))

        if score < 0.30:
            level = "very_low"
            instruction = (
                "The user's engagement is critically low. "
                "Do not continue the normal interview agenda. "
                "The interviewer should make a firm interaction-repair move. "
                "The interviewer may sound mildly annoyed, surprised, or disappointed, "
                "while remaining professional. "
                "Ask whether the candidate wants to continue the interview or stop here. "
                "Do not repeat the previous interview question."
            )
        elif score < 0.45:
            level = "low"
            instruction = (
                "The user's engagement is low. "
                "Do not simply repeat the previous question. "
                "Acknowledge that the interaction is not flowing naturally. "
                "Ask a shorter, more direct question, or ask whether the candidate "
                "wants to continue the interview. "
                "The interviewer may be firm but must remain professional."
            )
        elif score < 0.60:
            level = "medium"
            instruction = (
                "The user's engagement is moderate. "
                "Continue the interview, but keep the response concise and clear. "
                "Respond to the candidate's most recent utterance first. "
                "Ask only one question."
            )
        elif score < 0.80:
            level = "high"
            instruction = (
                "The user's engagement is good. "
                "Respond naturally to the candidate's most recent utterance, "
                "then continue the interview with a relevant follow-up."
            )
        else:
            level = "very_high"
            instruction = (
                "The user's engagement is very high. "
                "Respond to the candidate's most recent utterance and continue naturally. "
                "You may ask a more open follow-up or ask about availability to start."
            )

        age_seconds = None
        if latest_update_time is not None:
            age_seconds = time.time() - latest_update_time

        signals = [
            EngagementSignal(
                name="engagement_score",
                value=score,
                confidence=1.0,
            ),
            EngagementSignal(
                name="role",
                value=str(row.get("role")),
                confidence=None,
            ),
            EngagementSignal(
                name="gaze",
                value=str(row.get("gaze")),
                confidence=None,
            ),
            EngagementSignal(
                name="torso_position",
                value=str(row.get("torso_position")),
                confidence=None,
            ),
            EngagementSignal(
                name="smile",
                value=str(row.get("smile")),
                confidence=None,
            ),
        ]

        return EngagementState(
            level=level,
            score=score,
            signals=signals,
            summary=(
                f"Realtime engagement score is {score:.3f}. "
                f"{instruction}"
            ),
            metadata={
                "source": "realtime_subprocess",
                "ready": True,
                "latest_update_age_seconds": age_seconds,
                "log_path": str(self.log_path),
                "raw_row": row,
            },
        )