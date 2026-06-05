#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realtime_multimodal_engagement.py

Real-time multimodal engagement fusion:
- Webcam frame -> your VLM Engagement API
- Microphone audio -> Parselmouth prosody features
- Incremental engagement score -> OpenCV overlay + optional CSV log

Press q in the OpenCV window to quit.

Example:
  python realtime_multimodal_engagement.py \
    --url http://10.10.200.182/engagement_maria/engagement_maria \
    --camera 0 \
    --fps 3 \
    --fusion-fps 3 \
    --calib 5 \
    --out realtime_engagement_log.csv

Dependencies:
  pip install opencv-python requests pyyaml numpy sounddevice praat-parselmouth

Linux microphone note:
  If sounddevice cannot open the default mic, run:
    python -m sounddevice
  Then pass the desired index with:
    --audio-device <INDEX>
"""
from __future__ import annotations
import argparse
import base64
import csv
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
import cv2
import numpy as np
import requests
import sounddevice as sd
import yaml
import parselmouth
from collections import deque


CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "configs", "00-defaults.yml"
)

FIELD_ORDER = (
    "gaze",
    "torso_position",
    "feet_pointing",
    "crossed_arms",
    "sleepy_eyes",
    "smile",
    "eyebrow_raise",
)

ALLOWED_LABELS = {
    "gaze": {"camera", "away", "unclear"},
    "torso_position": {"towards", "away", "unclear"},
    "feet_pointing": {"towards", "away", "not_visible", "unclear"},
    "crossed_arms": {"yes", "no", "unclear"},
    "sleepy_eyes": {"yes", "no", "unclear"},
    "smile": {"yes", "no", "unclear"},
    "eyebrow_raise": {"yes", "no", "unclear"},
}

EPS = 1e-6

@dataclass
class Baseline:
    f0: float
    intensity: float
    rhythm: float

@dataclass
class ProsodyFeatures:
    f0: float
    intensity: float      # Parselmouth intensity, roughly dB SPL-like scale
    rhythm: float         # std of Parselmouth intensity contour, in dB
    rms: float            # waveform RMS, used for silence detection
    voiced_ratio: float   # proportion of pitch frames with f0 > 0


@dataclass
class VisualFeatures:
    gaze: Optional[str]
    torso_position: Optional[str]
    feet_pointing: Optional[str]
    crossed_arms: Optional[str]
    sleepy_eyes: Optional[str]
    smile: Optional[str]
    eyebrow_raise: Optional[str]
    raw: str
    ok: bool
    timestamp: float = 0.0
    status_code: Optional[int] = None
    latency: Optional[float] = None


@dataclass
class EngagementState:
    z: float
    e: float

@dataclass
class RoleGazeTracker:
    role: str = "listener"
    role_since: float = 0.0
    candidate_role: str = "listener"
    candidate_since: float = 0.0
    gaze_events: deque = field(default_factory=deque)

    def raw_role_from_prosody(
        self,
        pros: ProsodyFeatures,
        silence_rms_thresh: float,
        voiced_thresh: float = 0.20,
    ) -> str:
        has_reliable_f0 = (
            np.isfinite(pros.f0)
            and pros.rms >= silence_rms_thresh
            and pros.voiced_ratio >= voiced_thresh
        )
        return "speaker" if has_reliable_f0 else "listener"

    def update_role(
        self,
        raw_role: str,
        now: float,
        min_speaker_sec: float = 0.4,
        min_listener_sec: float = 1.0,
    ) -> None:
        if self.role_since == 0.0:
            self.role = raw_role
            self.candidate_role = raw_role
            self.role_since = now
            self.candidate_since = now
            return

        if raw_role == self.role:
            self.candidate_role = raw_role
            self.candidate_since = now
            return

        if raw_role != self.candidate_role:
            self.candidate_role = raw_role
            self.candidate_since = now
            return

        needed = min_speaker_sec if raw_role == "speaker" else min_listener_sec

        if now - self.candidate_since >= needed:
            self.role = raw_role
            self.role_since = now

    def add_gaze_sample(self, vis: VisualFeatures, now: float, dt: float) -> None:
        gaze = normalize_label(vis.gaze)

        if not vis.ok:
            return

        if gaze not in ("camera", "away"):
            return

        gaze_camera = 1.0 if gaze == "camera" else 0.0
        self.gaze_events.append((now, dt, self.role, gaze_camera))

    def gaze_ratio_for_current_role(
        self,
        now: float,
        speaker_window: float = 6.0,
        listener_window: float = 20.0,
        min_valid_speaker: float = 2.0,
        min_valid_listener: float = 5.0,
        speaker_threshold: float = 0.41,
        listener_threshold: float = 0.75,
    ) -> tuple[float, float, float, Optional[bool], float]:
        window = speaker_window if self.role == "speaker" else listener_window
        threshold = speaker_threshold if self.role == "speaker" else listener_threshold
        min_valid = min_valid_speaker if self.role == "speaker" else min_valid_listener

        start = max(self.role_since, now - window)

        # Limpieza conservadora: mantenemos un poco más que la ventana máxima.
        max_window = max(speaker_window, listener_window)
        while self.gaze_events and self.gaze_events[0][0] < now - max_window - 2.0:
            self.gaze_events.popleft()

        num = 0.0
        den = 0.0

        for t_i, dt_i, role_i, gaze_camera_i in self.gaze_events:
            if t_i >= start and role_i == self.role:
                num += dt_i * gaze_camera_i
                den += dt_i

        if den <= EPS:
            return np.nan, threshold, den, None, 0.0

        ratio = num / den

        if den < min_valid:
            return ratio, threshold, den, None, 0.0

        engaged = ratio >= threshold

        # Score continuo entre -1 y +1, con 0 justo en el umbral.
        if ratio >= threshold:
            score = (ratio - threshold) / max(1.0 - threshold, EPS)
        else:
            score = (ratio - threshold) / max(threshold, EPS)

        score = float(np.clip(score, -1.0, 1.0))
        return float(ratio), float(threshold), float(den), bool(engaged), score


def load_config_url() -> Optional[str]:
    if not os.path.exists(CONFIG_PATH):
        return None

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        params = cfg["/llm_bridge"]["ros__parameters"]
        base_url = params["server_workstation_url"].rstrip("/")
        endpoint = params["vlm_engagement_endpoint"]

        return base_url + endpoint

    except Exception as e:
        print(
            f"WARNING: Could not read config ({e}), using --url or fallback only",
            flush=True,
        )
        return None


def frame_to_base64(frame: np.ndarray, jpeg_quality: int = 70, max_width: int = 640) -> str:
    h, w = frame.shape[:2]
    if w > max_width:
        scale = max_width / float(w)
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    ok, buf = cv2.imencode(
        ".jpg",
        frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
    )
    if not ok:
        raise RuntimeError("Failed to JPEG-encode frame")
    return base64.b64encode(buf).decode("utf-8")


def normalize_label_value(x: Optional[Any]) -> Optional[str]:
    if x is None:
        return None
    x = str(x).strip().lower()
    x = x.replace("-", "_").replace(" ", "_")
    x = x.strip("\"'`")
    if x == "notvisible":
        x = "not_visible"
    return x


def normalize_label(x: Optional[str]) -> Optional[str]:
    if x is None:
        return None
    return x.strip().lower()


def clip01(x: float) -> float:
    return float(np.clip(x, 0.0, 1.0))


def sigmoid(x: float) -> float:
    return float(1.0 / (1.0 + np.exp(-x)))


def logit(p: float) -> float:
    p = float(np.clip(p, 1e-5, 1.0 - 1e-5))
    return float(np.log(p / (1.0 - p)))


def safe_log_ratio(x: float, base: float) -> float:
    if not np.isfinite(x) or not np.isfinite(base):
        return 0.0
    return float(np.log((x + EPS) / (base + EPS)))


def safe_db_delta(x: float, base: float, scale_db: float = 10.0) -> float:
    """For Parselmouth dB-like intensity/rhythm values: +10 dB -> roughly +1 evidence unit."""
    if not np.isfinite(x) or not np.isfinite(base):
        return 0.0
    return float((x - base) / max(scale_db, EPS))


def fmt_float(x: Optional[float], ndigits: int = 3) -> str:
    if x is None:
        return "None"
    try:
        if not np.isfinite(x):
            return "nan"
    except Exception:
        return str(x)
    return f"{float(x):.{ndigits}f}"


def score_gaze(label: Optional[str]) -> Optional[float]:
    lab = normalize_label(label)
    if lab == "camera":
        # Mirar a cámara suma muy poco: para la demo queremos evitar que
        # un frame positivo compense demasiado rápido señales negativas.
        return 0.5
    if lab == "away":
        # Penalización demo-friendly: gaze fuera de cámara debe bajar claro.
        return -0.35



def score_towards_away(label: Optional[str]) -> Optional[float]:
    lab = normalize_label(label)
    if lab == "towards":
        return 0.5
    if lab == "away":
        return -1.0
    return None


def score_torso_position(label: Optional[str]) -> Optional[float]:
    lab = normalize_label(label)
    if lab == "towards":
        return 0.35
    if lab == "away":
        return -1.10
    return None


def score_crossed_arms(label: Optional[str]) -> Optional[float]:
    lab = normalize_label(label)
    if lab == "yes":
        # Señal negativa fuerte para que se vea en demo.
        return -1
    if lab == "no":
        return 0.0
    return None


def score_sleepy_eyes(label: Optional[str]) -> Optional[float]:
    lab = normalize_label(label)
    if lab == "yes":
        # Sleepy eyes should have stronger negative evidence than ambiguous posture.
        return -1.65
    if lab == "no":
        return 0.0
    return None


def score_smile(label: Optional[str]) -> Optional[float]:
    lab = normalize_label(label)
    if lab == "yes":
        return 1.0
    if lab == "no":
        return 0.0
    return None


def score_eyebrow_raise(label: Optional[str]) -> Optional[float]:
    lab = normalize_label(label)
    if lab == "yes":
        return 1.0
    if lab == "no":
        return 0.0
    return None

# NOTE:
# This accumulator implements a heuristic, prototype-level fusion model.
# Weights are hand-tuned for real-time demonstration and should not be interpreted
# as validated psychological coefficients. User-based validation is required.

class EngagementAccumulator:
    def __init__(
        self,
        rho: float = 0.955,
        eta: float = 0.75,
        eta_up: float = 0.18,
        eta_down: float = 0.65,
        w_prosody: float = 0.55,
        w_visual: float = 0.45,
        a0: float = 1.2,
        a1: float = 0.8,
        a2: float = 0.6,
        silence_rms_thresh: float = 0.01,
        w_gaze: float = 1.6,
        w_torso: float = 0.9,
        w_feet: float = 0.4,
        w_crossed_arms: float = 1.45,
        w_sleepy_eyes: float = 1.65,
        w_smile: float = 0.45,
        w_eyebrow_raise: float = 0.35,
        hold_on_no_evidence: bool = True,
        normalize_visual: bool = True,
        center_pull: float = 0.035,
        habituation_grace_sec: float = 2.0,
        habituation_floor: float = 0.65,
        habituation_gaze: float = 0.04,
        habituation_torso: float = 0.08,
        habituation_feet: float = 0.08,
        habituation_crossed_arms: float = 0.06,
        habituation_sleepy_eyes: float = 0.05,
        habituation_smile: float = 0.12,
        habituation_eyebrow_raise: float = 0.12,
    ):
        self.rho = float(rho)
        self.eta = float(eta)
        self.eta_up = float(eta_up)
        self.eta_down = float(eta_down)
        self.w_prosody = float(w_prosody)
        self.w_visual = float(w_visual)
        self.a0 = float(a0)
        self.a1 = float(a1)
        self.a2 = float(a2)
        self.silence_rms_thresh = float(silence_rms_thresh)
        self.w_gaze = float(w_gaze)
        self.w_torso = float(w_torso)
        self.w_feet = float(w_feet)
        self.w_crossed_arms = float(w_crossed_arms)
        self.w_sleepy_eyes = float(w_sleepy_eyes)
        self.w_smile = float(w_smile)
        self.w_eyebrow_raise = float(w_eyebrow_raise)
        self.hold_on_no_evidence = bool(hold_on_no_evidence)
        self.normalize_visual = bool(normalize_visual)
        self.center_pull = float(center_pull)
        self.habituation_grace_sec = float(habituation_grace_sec)
        self.habituation_floor = float(np.clip(habituation_floor, 0.0, 1.0))
        self.habituation_rates = {
            "gaze": float(habituation_gaze),
            "torso_position": float(habituation_torso),
            "feet_pointing": float(habituation_feet),
            "crossed_arms": float(habituation_crossed_arms),
            "sleepy_eyes": float(habituation_sleepy_eyes),
            "smile": float(habituation_smile),
            "eyebrow_raise": float(habituation_eyebrow_raise),
        }
        self._visual_prev = {field: None for field in FIELD_ORDER}
        self._visual_duration_sec = {field: 0.0 for field in FIELD_ORDER}

    def observe_visual(self, vis: VisualFeatures, dt_sec: float) -> None:
        dt_sec = max(float(dt_sec), 0.0)
        for field in FIELD_ORDER:
            lab = normalize_label(getattr(vis, field))
            if lab is None or lab == "unclear" or (field == "feet_pointing" and lab == "not_visible"):
                self._visual_prev[field] = None
                self._visual_duration_sec[field] = 0.0
                continue
            if self._visual_prev[field] == lab:
                self._visual_duration_sec[field] += dt_sec
            else:
                self._visual_prev[field] = lab
                self._visual_duration_sec[field] = dt_sec

    def _effective_visual_weight(self, field: str, weight: float, label: Optional[str], score: Optional[float]) -> float:
        if weight <= 0.0 or score is None or abs(score) <= EPS:
            return float(weight)
        lab = normalize_label(label)
        if lab is None or lab == "unclear" or (field == "feet_pointing" and lab == "not_visible"):
            return float(weight)
        rate = self.habituation_rates.get(field, 0.0)
        if rate <= 0.0:
            return float(weight)
        duration_sec = self._visual_duration_sec.get(field, 0.0)
        effective_duration = max(0.0, duration_sec - self.habituation_grace_sec)
        decay = float(np.exp(-rate * effective_duration))
        decay = max(self.habituation_floor, decay)
        return float(weight * decay)

    def prosody_active(self, pros: ProsodyFeatures) -> bool:
        return pros.rms >= self.silence_rms_thresh

    def prosody_reliability(self, pros: ProsodyFeatures) -> float:
        if not self.prosody_active(pros):
            return 0.0
        r_rms = float(np.clip(pros.rms / (self.silence_rms_thresh + EPS), 0.0, 1.0))
        r_voiced = float(np.clip(pros.voiced_ratio / 0.5, 0.0, 1.0))
        return float(0.7 * r_rms + 0.3 * r_voiced)

    def visual_evidence_and_availability(
        self,
        vis: VisualFeatures,
        gaze_score_override: Optional[float] = None,
        use_gaze_override: bool = False,
    ) -> Tuple[float, float]:
        if not vis.ok:
            return 0.0, 0.0
        pairs = []
        available_weight = 0.0
        possible_weight = 0.0

        def consider(field: str, weight: float, label: Optional[str], score: Optional[float], hidden_labels=()):
            nonlocal available_weight, possible_weight
            if weight <= 0:
                return
            lab = normalize_label(label)
            if lab in hidden_labels:
                return
            possible_weight += weight
            if score is None:
                return
            available_weight += weight
            effective_weight = self._effective_visual_weight(field, weight, label, score)
            pairs.append((effective_weight, score))

        if use_gaze_override:
            consider("role_gaze", self.w_gaze, "role_gaze", gaze_score_override)
        else:
            consider("gaze", self.w_gaze, vis.gaze, score_gaze(vis.gaze))

        # Always include the rest of the visual channels.
        consider("torso_position", self.w_torso, vis.torso_position, score_torso_position(vis.torso_position))
        consider("feet_pointing", self.w_feet, vis.feet_pointing, score_towards_away(vis.feet_pointing), hidden_labels=("not_visible",))
        consider("crossed_arms", self.w_crossed_arms, vis.crossed_arms, score_crossed_arms(vis.crossed_arms))
        consider("sleepy_eyes", self.w_sleepy_eyes, vis.sleepy_eyes, score_sleepy_eyes(vis.sleepy_eyes))
        consider("smile", self.w_smile, vis.smile, score_smile(vis.smile))
        consider("eyebrow_raise", self.w_eyebrow_raise, vis.eyebrow_raise, score_eyebrow_raise(vis.eyebrow_raise))

        if not pairs:
            return 0.0, 0.0
        weighted_sum = float(sum(w * s for w, s in pairs))
        if self.normalize_visual and available_weight > EPS:
            v_t = weighted_sum / available_weight
        else:
            v_t = weighted_sum
        avail_ratio = 0.0 if possible_weight <= EPS else float(np.clip(available_weight / possible_weight, 0.0, 1.0))
        return float(v_t), avail_ratio

    def reliability(
        self,
        vis: VisualFeatures,
        pros: ProsodyFeatures,
        gaze_score_override: Optional[float] = None,
        use_gaze_override: bool = False,
    ) -> float:
        active_reliabilities = []
        r_pro = self.prosody_reliability(pros)
        if r_pro > 0.0:
            active_reliabilities.append(r_pro)
        _, r_vis = self.visual_evidence_and_availability(
            vis,
            gaze_score_override=gaze_score_override,
            use_gaze_override=use_gaze_override,
        )
        if r_vis > 0.0:
            active_reliabilities.append(r_vis)
        if not active_reliabilities:
            return 0.0
        return float(np.mean(active_reliabilities))

    def evidence_score(
        self,
        dF0: float,
        dI: float,
        dR: float,
        pros: ProsodyFeatures,
        vis: VisualFeatures,
        gaze_score_override: Optional[float] = None,
        use_gaze_override: bool = False,
    ) -> Tuple[float, float, float]:
        if self.prosody_active(pros):
            p_t = self.a0 * dI + self.a1 * dR + self.a2 * abs(dF0)
        else:
            p_t = 0.0
        v_t, _ = self.visual_evidence_and_availability(
            vis,
            gaze_score_override=gaze_score_override,
            use_gaze_override=use_gaze_override,
        )
        s_t = self.w_prosody * p_t + self.w_visual * v_t
        return float(s_t), float(p_t), float(v_t)

    def update(self, state: EngagementState, r_t: float, s_t: float) -> EngagementState:
        pull_to_center = self.center_pull * np.tanh(state.z)

        if self.hold_on_no_evidence and (r_t <= 0.0 or abs(s_t) <= EPS):
            z_new = state.z - pull_to_center
            return EngagementState(z=float(z_new), e=sigmoid(z_new))

        # Negative evidence should move faster than positive evidence.
        if s_t < 0.0:
            eta = self.eta_down
        else:
            eta = self.eta_up

        z_new = self.rho * state.z + eta * r_t * s_t - pull_to_center
        return EngagementState(z=float(z_new), e=sigmoid(z_new))


class AudioRingBuffer:
    def __init__(self, sr: int, seconds: float):
        self.sr = int(sr)
        self.size = max(int(sr * seconds), 1)
        self.buffer = np.zeros(self.size, dtype=np.float32)
        self.write_idx = 0
        self.total_samples = 0
        self.lock = threading.Lock()

    def add(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        with self.lock:
            if samples.size >= self.size:
                self.buffer[:] = samples[-self.size:]
                self.write_idx = 0
                self.total_samples += samples.size
                return
            n = samples.size
            end = self.write_idx + n
            if end <= self.size:
                self.buffer[self.write_idx:end] = samples
            else:
                first = self.size - self.write_idx
                self.buffer[self.write_idx:] = samples[:first]
                self.buffer[:end % self.size] = samples[first:]
            self.write_idx = end % self.size
            self.total_samples += n

    def ready(self, seconds: float) -> bool:
        return self.total_samples >= int(seconds * self.sr)

    def get_last(self, seconds: float) -> Optional[np.ndarray]:
        n = int(seconds * self.sr)
        if n <= 0:
            return None
        with self.lock:
            available = min(self.total_samples, self.size)
            if available < n:
                return None
            start = (self.write_idx - n) % self.size
            if start < self.write_idx:
                return self.buffer[start:self.write_idx].copy()
            return np.concatenate((self.buffer[start:], self.buffer[:self.write_idx])).copy()


def make_audio_callback(ring: AudioRingBuffer):
    def callback(indata, frames, time_info, status):
        if status:
            # Do not print on every callback; status can be noisy on busy systems.
            pass
        ring.add(indata[:, 0])
    return callback


def coerce_audio_device(value):
    """Convert numeric CLI values like --audio-device 10 into int(10)."""
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return s


def audio_level(y: Optional[np.ndarray]) -> Tuple[float, float]:
    if y is None or len(y) == 0:
        return 0.0, 0.0
    y = np.asarray(y, dtype=np.float32).reshape(-1)
    return float(np.sqrt(np.mean(np.square(y)))), float(np.max(np.abs(y)))


def analyze_prosody_parselmouth(
    y: Optional[np.ndarray],
    sr: int,
    pitch_floor: float = 75.0,
    pitch_ceiling: float = 500.0,
    silence_rms_thresh: float = 0.01,
) -> ProsodyFeatures:
    if y is None or len(y) < int(0.2 * sr):
        return ProsodyFeatures(f0=np.nan, intensity=0.0, rhythm=0.0, rms=0.0, voiced_ratio=0.0)

    y = np.asarray(y, dtype=np.float64).reshape(-1)
    y = y - float(np.mean(y))
    rms = float(np.sqrt(np.mean(np.square(y))) + EPS)

    if rms < silence_rms_thresh * 0.35:
        return ProsodyFeatures(f0=np.nan, intensity=0.0, rhythm=0.0, rms=rms, voiced_ratio=0.0)

    try:
        snd = parselmouth.Sound(y, sampling_frequency=sr)

        pitch = snd.to_pitch(
            time_step=0.01,
            pitch_floor=float(pitch_floor),
            pitch_ceiling=float(pitch_ceiling),
        )
        f0 = np.asarray(pitch.selected_array["frequency"], dtype=np.float64)
        voiced = np.isfinite(f0) & (f0 > 0.0)
        voiced_ratio = float(np.mean(voiced)) if f0.size else 0.0
        f0_mean = float(np.mean(f0[voiced])) if np.any(voiced) else np.nan

        intensity_obj = snd.to_intensity(
            time_step=0.01,
            minimum_pitch=float(pitch_floor),
        )
        intensity_values = np.asarray(intensity_obj.values, dtype=np.float64).reshape(-1)
        intensity_values = intensity_values[np.isfinite(intensity_values)]
        if intensity_values.size:
            intensity_mean = float(np.mean(intensity_values))
            rhythm = float(np.std(intensity_values))
        else:
            intensity_mean = 0.0
            rhythm = 0.0

        return ProsodyFeatures(
            f0=f0_mean,
            intensity=intensity_mean,
            rhythm=rhythm,
            rms=rms,
            voiced_ratio=voiced_ratio,
        )
    except Exception:
        return ProsodyFeatures(f0=np.nan, intensity=0.0, rhythm=0.0, rms=rms, voiced_ratio=0.0)


def calibrate_baseline_from_audio(
    y: np.ndarray,
    sr: int,
    silence_rms_thresh: float,
    pitch_floor: float,
    pitch_ceiling: float,
) -> Baseline:
    f0_vals = []
    int_vals = []
    rhy_vals = []
    win = int(sr)

    for start in range(0, max(len(y) - win + 1, 0), win):
        seg = y[start:start + win]
        pros = analyze_prosody_parselmouth(
            seg,
            sr,
            pitch_floor=pitch_floor,
            pitch_ceiling=pitch_ceiling,
            silence_rms_thresh=silence_rms_thresh,
        )
        if pros.rms > silence_rms_thresh and pros.voiced_ratio > 0.2:
            if np.isfinite(pros.f0):
                f0_vals.append(pros.f0)
            if np.isfinite(pros.intensity) and pros.intensity > 0.0:
                int_vals.append(pros.intensity)
            if np.isfinite(pros.rhythm):
                rhy_vals.append(pros.rhythm)

    # Fallbacks are intentionally neutral-ish so the script still runs if the user is silent during calibration.
    if not f0_vals:
        f0_vals = [180.0]
    if not int_vals:
        int_vals = [55.0]
    if not rhy_vals:
        rhy_vals = [2.0]

    return Baseline(
        f0=float(np.mean(f0_vals)),
        intensity=float(np.mean(int_vals)),
        rhythm=float(np.mean(rhy_vals)),
    )


def baseline_drift_update(
    base: Baseline,
    pros: ProsodyFeatures,
    lam: float = 0.01,
    rms_thresh: float = 0.01,
) -> Baseline:
    if pros.rms < rms_thresh or pros.voiced_ratio < 0.15:
        return base
    if not np.isfinite(pros.f0):
        return base
    f0_new = (1.0 - lam) * base.f0 + lam * pros.f0
    i_new = (1.0 - lam) * base.intensity + lam * pros.intensity
    r_new = (1.0 - lam) * base.rhythm + lam * pros.rhythm
    return Baseline(f0=float(f0_new), intensity=float(i_new), rhythm=float(r_new))


def prosody_deltas(pros: ProsodyFeatures, baseline: Baseline) -> Tuple[float, float, float]:
    dF0 = safe_log_ratio(pros.f0, baseline.f0)
    dI = safe_db_delta(pros.intensity, baseline.intensity, scale_db=10.0)
    dR = safe_db_delta(pros.rhythm, baseline.rhythm, scale_db=10.0)
    return dF0, dI, dR


def find_visual_dict(data: Any) -> Optional[Dict[str, Any]]:
    if isinstance(data, dict):
        if any(k in data for k in FIELD_ORDER):
            return data
        for value in data.values():
            found = find_visual_dict(value)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_visual_dict(item)
            if found is not None:
                return found
    return None


def visual_from_api_json(data: Any, status_code: Optional[int], latency: Optional[float]) -> VisualFeatures:
    d = find_visual_dict(data)
    raw = json.dumps(data, ensure_ascii=False)[:2000]
    if d is None:
        return VisualFeatures(None, None, None, None, None, None, None, raw=raw, ok=False, timestamp=time.time(), status_code=status_code, latency=latency)

    values = {}
    any_valid = False
    for field in FIELD_ORDER:
        lab = normalize_label_value(d.get(field))
        if lab in ALLOWED_LABELS[field]:
            values[field] = lab
            any_valid = True
        else:
            values[field] = None

    return VisualFeatures(
        gaze=values["gaze"],
        torso_position=values["torso_position"],
        feet_pointing=values["feet_pointing"],
        crossed_arms=values["crossed_arms"],
        sleepy_eyes=values["sleepy_eyes"],
        smile=values["smile"],
        eyebrow_raise=values["eyebrow_raise"],
        raw=raw,
        ok=any_valid,
        timestamp=time.time(),
        status_code=status_code,
        latency=latency,
    )


class APIVisualWorker:
    def __init__(self, url: str, dataset: str, verbose: bool, quality: int, max_width: int, timeout: float):
        self.url = url
        self.dataset = dataset
        self.verbose = bool(verbose)
        self.quality = int(quality)
        self.max_width = int(max_width)
        self.timeout = float(timeout)
        self.q: "queue.Queue[Tuple[int, np.ndarray]]" = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest_visual = VisualFeatures(None, None, None, None, None, None, None, raw="", ok=False, timestamp=0.0)
        self.latest_frame_id = 0
        self.busy = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.q.put_nowait((-1, np.zeros((1, 1, 3), dtype=np.uint8)))
        except queue.Full:
            pass
        self.thread.join(timeout=2.0)

    def submit_latest(self, frame_id: int, frame: np.ndarray) -> bool:
        if self.q.full():
            return False
        try:
            self.q.put_nowait((frame_id, frame.copy()))
            return True
        except queue.Full:
            return False

    def get_latest(self) -> Tuple[VisualFeatures, int, bool]:
        with self.lock:
            return self.latest_visual, self.latest_frame_id, self.busy

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame_id, frame = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            if frame_id < 0:
                break
            with self.lock:
                self.busy = True
            try:
                b64 = frame_to_base64(frame, jpeg_quality=self.quality, max_width=self.max_width)
                payload = {
                    "images": [b64],
                    "dataset": self.dataset,
                    "verbose_output": self.verbose,
                }
                t0 = time.time()
                r = requests.post(self.url, json=payload, timeout=self.timeout)
                latency = time.time() - t0
                if r.ok:
                    try:
                        data = r.json()
                    except Exception:
                        data = {"error": "non_json_response", "text": r.text[:500]}
                    vis = visual_from_api_json(data, status_code=r.status_code, latency=latency)
                else:
                    try:
                        data = r.json()
                    except Exception:
                        data = {"error": "http_error", "text": r.text[:500]}
                    vis = visual_from_api_json(data, status_code=r.status_code, latency=latency)
                    vis.ok = False
                with self.lock:
                    self.latest_visual = vis
                    self.latest_frame_id = frame_id
            except requests.exceptions.Timeout:
                vis = VisualFeatures(None, None, None, None, None, None, None, raw="VLM_TIMEOUT", ok=False, timestamp=time.time(), status_code=None, latency=self.timeout)
                with self.lock:
                    self.latest_visual = vis
                    self.latest_frame_id = frame_id
            except requests.exceptions.ConnectionError:
                vis = VisualFeatures(None, None, None, None, None, None, None, raw="VLM_CONNECTION_ERROR", ok=False, timestamp=time.time())
                with self.lock:
                    self.latest_visual = vis
                    self.latest_frame_id = frame_id
            except Exception as e:
                vis = VisualFeatures(None, None, None, None, None, None, None, raw=f"VLM_ERROR: {e}", ok=False, timestamp=time.time())
                with self.lock:
                    self.latest_visual = vis
                    self.latest_frame_id = frame_id
            finally:
                with self.lock:
                    self.busy = False
                self.q.task_done()


def draw_overlay(
    frame: np.ndarray,
    state: EngagementState,
    pros: ProsodyFeatures,
    vis: VisualFeatures,
    baseline: Optional[Baseline],
    p_t: float,
    v_t: float,
    r_t: float,
    calibrating: bool,
    api_busy: bool,
    visual_age: float,
) -> np.ndarray:
    """Compact top-left overlay for demos.

    The previous debug overlay was useful, but it covered the participant's face.
    This version keeps only the live values that matter during the demo and uses
    a much smaller box anchored to the very top-left of the frame.
    """
    overlay = frame.copy()
    h, w = overlay.shape[:2]

    # Small, high panel: avoid covering eyes/face as much as possible.
    box_w = min(370, w - 12)
    box_h = 132
    x0, y0 = 6, 4
    x1, y1 = x0 + box_w, y0 + box_h

    cv2.rectangle(overlay, (x0, y0), (x1, y1), (0, 0, 0), -1)
    # A bit more transparent than before so the face/background remains visible.
    cv2.addWeighted(overlay, 0.52, frame, 0.48, 0, frame)

    e = state.e
    if e >= 0.66:
        color = (40, 220, 40)
    elif e <= 0.33:
        color = (40, 40, 220)
    else:
        color = (0, 220, 220)

    status = "CALIBRATING" if calibrating else "RUNNING"
    lat = "nan" if vis.latency is None else f"{vis.latency:.2f}s"

    y = y0 + 25
    cv2.putText(
        frame,
        f"Engagement: {e:.3f}",
        (x0 + 10, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.68,
        color,
        2,
        cv2.LINE_AA,
    )

    y += 22
    cv2.putText(
        frame,
        f"{status} | API busy={api_busy} | age={visual_age:.1f}s",
        (x0 + 10, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.43,
        (235, 235, 235),
        1,
        cv2.LINE_AA,
    )

    y += 20
    cv2.putText(
        frame,
        f"gaze={vis.gaze}  torso={vis.torso_position}  feet={vis.feet_pointing}",
        (x0 + 10, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.42,
        (235, 235, 235),
        1,
        cv2.LINE_AA,
    )

    y += 20
    cv2.putText(
        frame,
        f"arms={vis.crossed_arms}  sleepy={vis.sleepy_eyes}  smile={vis.smile}",
        (x0 + 10, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.42,
        (235, 235, 235),
        1,
        cv2.LINE_AA,
    )

    y += 20
    cv2.putText(
        frame,
        f"p={p_t:+.2f} v={v_t:+.2f} r={r_t:.2f} | HTTP={vis.status_code} lat={lat}",
        (x0 + 10, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.40,
        (220, 220, 220),
        1,
        cv2.LINE_AA,
    )

    bar_x0, bar_y0 = x0 + 10, y1 - 13
    bar_x1 = x1 - 10
    cv2.rectangle(frame, (bar_x0, bar_y0), (bar_x1, bar_y0 + 6), (70, 70, 70), -1)
    cv2.rectangle(
        frame,
        (bar_x0, bar_y0),
        (bar_x0 + int((bar_x1 - bar_x0) * e), bar_y0 + 6),
        color,
        -1,
    )
    return frame

def open_csv_writer(path: Optional[str]):
    if not path:
        return None, None
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(out_path, "w", newline="", encoding="utf-8")
    fieldnames = [
        "wall_time",
        "frame_id",
        "f0",
        "intensity_db",
        "rhythm_db_std",
        "rms",
        "voiced_ratio",
        "dF0",
        "dI",
        "dR",
        "role",
        "raw_role",
        "gaze",
        "gaze_ratio_role_window",
        "gaze_threshold",
        "gaze_valid_sec",
        "gaze_engaged_by_role",
        "gaze_role_score",
        "gaze_score_for_fusion",
        "torso_position",
        "feet_pointing",
        "crossed_arms",
        "sleepy_eyes",
        "smile",
        "eyebrow_raise",
        "visual_ok",
        "visual_age",
        "http_status",
        "api_latency",
        "p_t",
        "v_t",
        "r_t",
        "s_t",
        "engagement",
    ]
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    return f, writer


def parse_args():
    default_url = load_config_url()
    parser = argparse.ArgumentParser(description="Real-time audio+VLM engagement fusion")
    parser.add_argument("--url", type=str, default=default_url, help="VLM Engagement API endpoint URL")
    parser.add_argument("--dataset", type=str, default="debugging", choices=["therapy", "debugging"])
    parser.add_argument("--verbose", action="store_true", help="Ask API for verbose output")
    parser.add_argument("--quality", type=int, default=70, help="JPEG quality for API frames")
    parser.add_argument("--fps", type=float, default=4.0, help="Frames per second sent to the API")
    parser.add_argument("--fusion-fps", type=float, default=3.0, help="Engagement updates per second")
    parser.add_argument("--timeout", type=float, default=10.0, help="API request timeout")
    parser.add_argument("--camera", type=int, default=0, help="OpenCV camera index")
    parser.add_argument("--max-width", type=int, default=640, help="Resize frames before API call")
    parser.add_argument("--max-visual-age", type=float, default=2.5, help="Ignore visual result if older than this many seconds")

    parser.add_argument("--sr", type=int, default=44100, help="Microphone sample rate")
    parser.add_argument("--audio-device", default=None, help="sounddevice input device index/name; run `python -m sounddevice` to list")
    parser.add_argument("--audio-window", type=float, default=1.0, help="Seconds of audio used for each prosody estimate")
    parser.add_argument("--ring-seconds", type=float, default=12.0, help="Audio ring buffer length")
    parser.add_argument("--calib", type=float, default=5.0, help="Initial microphone calibration seconds")
    parser.add_argument("--silence-rms-thresh", type=float, default=0.01, help="RMS threshold for speech activity")
    parser.add_argument("--pitch-floor", type=float, default=75.0)
    parser.add_argument("--pitch-ceiling", type=float, default=500.0)
    parser.add_argument("--audio-debug", action="store_true", help="Print live mic RMS/peak once per second")

    parser.add_argument("--out", type=str, default="realtime_engagement_log.csv", help="CSV log path; use empty string to disable")
    parser.add_argument("--print-format", choices=["text", "json", "none"], default="text")
    parser.add_argument("--no-window", action="store_true", help="Do not open OpenCV window")

    parser.add_argument("--rho", type=float, default=0.96)
    parser.add_argument("--eta", type=float, default=0.35)
    parser.add_argument("--eta-up", type=float, default=0.18)
    parser.add_argument("--eta-down", type=float, default=0.65)
    parser.add_argument("--w-prosody", type=float, default=0.45)
    parser.add_argument("--w-visual", type=float, default=0.55)
    parser.add_argument("--hold-on-no-evidence", type=int, default=1)
    parser.add_argument("--w-intensity", type=float, default=1.2)
    parser.add_argument("--w-rhythm", type=float, default=0.8)
    parser.add_argument("--w-f0", type=float, default=0.8)
    parser.add_argument("--w-gaze", type=float, default=0.9)
    parser.add_argument("--w-torso", type=float, default=1.0)
    parser.add_argument("--w-feet", type=float, default=0.4)
    parser.add_argument("--w-crossed-arms", type=float, default=0.45)
    parser.add_argument("--w-sleepy-eyes", type=float, default=0.55)
    parser.add_argument("--w-smile", type=float, default=1.1)
    parser.add_argument("--w-eyebrow-raise", type=float, default=0.8)
    parser.add_argument("--center-pull", type=float, default=0.012)
    parser.add_argument("--habituation-grace-sec", type=float, default=4.0)
    parser.add_argument("--habituation-floor", type=float, default=0.65)
    parser.add_argument("--role-voiced-thresh", type=float, default=0.20,
                        help="Minimum voiced_ratio to classify the user as speaker")
    parser.add_argument("--role-min-speaker-sec", type=float, default=0.4,
                        help="Debounce: seconds of speech before switching to speaker")
    parser.add_argument("--role-min-listener-sec", type=float, default=1.0,
                        help="Debounce: seconds without speech before switching to listener")
    parser.add_argument("--speaker-gaze-window", type=float, default=4.0,
                        help="Seconds used for gaze ratio while role=speaker")
    parser.add_argument("--listener-gaze-window", type=float, default=8.0,
                        help="Seconds used for gaze ratio while role=listener")
    parser.add_argument("--min-valid-speaker-gaze", type=float, default=1.0,
                        help="Minimum valid gaze seconds before speaker gaze affects fusion")
    parser.add_argument("--min-valid-listener-gaze", type=float, default=1.0,
                        help="Minimum valid gaze seconds before listener gaze affects fusion")
    parser.add_argument("--speaker-gaze-thresh", type=float, default=0.35,
                        help="Gaze-camera ratio needed to be engaged while speaker")
    parser.add_argument("--listener-gaze-thresh", type=float, default=0.50,
                        help="Gaze-camera ratio needed to be engaged while listener")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.url:
        print("ERROR: No URL provided and config URL could not be loaded. Pass --url http://...", file=sys.stderr)
        return 1

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"ERROR: Could not open webcam index {args.camera}", file=sys.stderr)
        return 1

    audio_device = coerce_audio_device(args.audio_device)
    try:
        audio_info = sd.query_devices(audio_device, "input")
        print(
            f"Audio input: index={audio_info.get('index')} name={audio_info.get('name')} "
            f"max_input_channels={audio_info.get('max_input_channels')} "
            f"default_samplerate={audio_info.get('default_samplerate')} requested_sr={args.sr}",
            flush=True,
        )
    except Exception as e:
        print(f"WARNING: Could not query audio device {audio_device!r}: {e}", flush=True)

    ring = AudioRingBuffer(sr=args.sr, seconds=args.ring_seconds)

    model = EngagementAccumulator(
        rho=args.rho,
        eta=args.eta,
        eta_up=args.eta_up,
        eta_down=args.eta_down,
        w_prosody=args.w_prosody,
        w_visual=args.w_visual,
        a0=args.w_intensity,
        a1=args.w_rhythm,
        a2=args.w_f0,
        silence_rms_thresh=args.silence_rms_thresh,
        w_gaze=args.w_gaze,
        w_torso=args.w_torso,
        w_feet=args.w_feet,
        w_crossed_arms=args.w_crossed_arms,
        w_sleepy_eyes=args.w_sleepy_eyes,
        w_smile=args.w_smile,
        w_eyebrow_raise=args.w_eyebrow_raise,
        hold_on_no_evidence=bool(args.hold_on_no_evidence),
        normalize_visual=True,
        center_pull=args.center_pull,
        habituation_grace_sec=args.habituation_grace_sec,
        habituation_floor=args.habituation_floor,
    )
    state = EngagementState(z=logit(0.5), e=0.5)
    role_gaze = RoleGazeTracker()
    worker = APIVisualWorker(
        url=args.url,
        dataset=args.dataset,
        verbose=args.verbose,
        quality=args.quality,
        max_width=args.max_width,
        timeout=args.timeout,
    )
    worker.start()

    csv_f, csv_writer = open_csv_writer(args.out if args.out else None)

    print(f"Streaming VLM API to: {args.url}", flush=True)
    print(f"Camera index: {args.camera} | API FPS: {args.fps} | Fusion FPS: {args.fusion_fps}", flush=True)
    print("Press q in the OpenCV window to quit.", flush=True)

    baseline: Optional[Baseline] = None
    start_time = time.time()
    last_api_submit = 0.0
    last_fusion = 0.0
    last_audio_debug = 0.0
    last_fusion_dt = 1.0 / max(float(args.fusion_fps), EPS)
    frame_id = 0

    latest_pros = ProsodyFeatures(f0=np.nan, intensity=0.0, rhythm=0.0, rms=0.0, voiced_ratio=0.0)
    latest_p_t = 0.0
    latest_v_t = 0.0
    latest_r_t = 0.0
    latest_s_t = 0.0

    try:
        with sd.InputStream(
            samplerate=args.sr,
            channels=1,
            dtype="float32",
            device=audio_device,
            blocksize=int(args.sr * 0.05),
            callback=make_audio_callback(ring),
        ):
            while True:
                ret, frame = cap.read()
                if not ret:
                    print("WARNING: Failed to read frame from camera", flush=True)
                    time.sleep(0.02)
                    continue

                now = time.time()
                elapsed = now - start_time
                frame_id += 1

                if args.audio_debug and now - last_audio_debug >= 1.0:
                    last_audio_debug = now
                    y_dbg = ring.get_last(min(1.0, args.audio_window))
                    rms_dbg, peak_dbg = audio_level(y_dbg)
                    print(
                        f"AUDIO_DEBUG device={audio_device!r} sr={args.sr} "
                        f"samples={ring.total_samples} rms={rms_dbg:.6f} peak={peak_dbg:.6f}",
                        flush=True,
                    )

                if now - last_api_submit >= 1.0 / max(float(args.fps), EPS):
                    if worker.submit_latest(frame_id, frame):
                        last_api_submit = now

                vis, latest_visual_frame_id, api_busy = worker.get_latest()
                visual_age = now - vis.timestamp if vis.timestamp > 0 else 999.0
                if visual_age > args.max_visual_age:
                    vis_for_fusion = VisualFeatures(None, None, None, None, None, None, None, raw="STALE_VISUAL", ok=False, timestamp=vis.timestamp, status_code=vis.status_code, latency=vis.latency)
                else:
                    vis_for_fusion = vis

                calibrating = baseline is None
                if baseline is None and elapsed >= args.calib and ring.ready(args.calib):
                    calib_audio = ring.get_last(args.calib)
                    if calib_audio is not None:
                        baseline = calibrate_baseline_from_audio(
                            calib_audio,
                            args.sr,
                            silence_rms_thresh=args.silence_rms_thresh,
                            pitch_floor=args.pitch_floor,
                            pitch_ceiling=args.pitch_ceiling,
                        )
                        print(
                            f"Baseline calibrated: f0={baseline.f0:.1f}Hz intensity={baseline.intensity:.1f}dB rhythm={baseline.rhythm:.2f}",
                            flush=True,
                        )
                        calibrating = False

                if baseline is not None and now - last_fusion >= last_fusion_dt:
                    dt = now - last_fusion if last_fusion > 0 else last_fusion_dt
                    last_fusion = now

                    # Defensive defaults for the first fusion tick and any partial
                    # visual/API state. These prevent UnboundLocalError if a future
                    # edit skips part of the evidence path.
                    s_t = 0.0
                    raw_role = role_gaze.role
                    dF0 = dI = dR = 0.0
                    gaze_ratio = np.nan
                    gaze_threshold = np.nan
                    gaze_valid_sec = 0.0
                    gaze_engaged = None
                    gaze_role_score = 0.0
                    gaze_score_for_fusion = None

                    y = ring.get_last(args.audio_window)
                    latest_pros = analyze_prosody_parselmouth(
                        y,
                        args.sr,
                        pitch_floor=args.pitch_floor,
                        pitch_ceiling=args.pitch_ceiling,
                        silence_rms_thresh=args.silence_rms_thresh,
                    )
                    dF0, dI, dR = prosody_deltas(latest_pros, baseline)

                    raw_role = role_gaze.raw_role_from_prosody(
                        latest_pros,
                        silence_rms_thresh=args.silence_rms_thresh,
                        voiced_thresh=args.role_voiced_thresh,
                    )

                    role_gaze.update_role(
                        raw_role=raw_role,
                        now=now,
                        min_speaker_sec=args.role_min_speaker_sec,
                        min_listener_sec=args.role_min_listener_sec,
                    )

                    role_gaze.add_gaze_sample(
                        vis=vis_for_fusion,
                        now=now,
                        dt=dt,
                    )

                    gaze_ratio, gaze_threshold, gaze_valid_sec, gaze_engaged, gaze_role_score = (
                        role_gaze.gaze_ratio_for_current_role(
                            now=now,
                            speaker_window=args.speaker_gaze_window,
                            listener_window=args.listener_gaze_window,
                            min_valid_speaker=args.min_valid_speaker_gaze,
                            min_valid_listener=args.min_valid_listener_gaze,
                            speaker_threshold=args.speaker_gaze_thresh,
                            listener_threshold=args.listener_gaze_thresh,
                        )
                    )

                    if gaze_engaged is None:
                        gaze_score_for_fusion = None
                    else:
                        if role_gaze.role == "listener":
                            gaze_score_for_fusion = 0.45 * max(gaze_role_score, 0.0) + 0.45 * min(gaze_role_score, 0.0)
                        else:
                            gaze_score_for_fusion = 0.55 * max(gaze_role_score, 0.0) + 0.45 * min(gaze_role_score, 0.0)

                    # Demo responsiveness: si el frame actual ya dice gaze=away,
                    # no esperamos a que toda la ventana role-gaze se llene para
                    # empezar a bajar. Usamos el mínimo para no suavizar de más.
                    if normalize_label(vis_for_fusion.gaze) == "away":
                        instant_away_score = score_gaze("away")
                        if gaze_score_for_fusion is None:
                            gaze_score_for_fusion = instant_away_score
                        else:
                            gaze_score_for_fusion = min(gaze_score_for_fusion, instant_away_score)

                    model.observe_visual(vis_for_fusion, dt_sec=dt)
                    latest_r_t = model.reliability(
                        vis=vis_for_fusion,
                        pros=latest_pros,
                        gaze_score_override=gaze_score_for_fusion,
                        use_gaze_override=True,
                    )

                    s_t, latest_p_t, latest_v_t = model.evidence_score(
                        dF0=dF0,
                        dI=dI,
                        dR=dR,
                        pros=latest_pros,
                        vis=vis_for_fusion,
                        gaze_score_override=gaze_score_for_fusion,
                        use_gaze_override=True,
                    )

                    latest_s_t = float(s_t)

                    state = model.update(state, r_t=latest_r_t, s_t=latest_s_t)
                    baseline = baseline_drift_update(baseline, latest_pros, lam=0.01, rms_thresh=args.silence_rms_thresh)

                    row = {
                        "wall_time": round(now, 3),
                        "frame_id": frame_id,
                        "f0": latest_pros.f0,
                        "intensity_db": latest_pros.intensity,
                        "rhythm_db_std": latest_pros.rhythm,
                        "rms": latest_pros.rms,
                        "voiced_ratio": latest_pros.voiced_ratio,
                        "dF0": dF0,
                        "dI": dI,
                        "dR": dR,
                        "role": role_gaze.role,
                        "raw_role": raw_role,
                        "gaze_ratio_role_window": gaze_ratio,
                        "gaze_threshold": gaze_threshold,
                        "gaze_valid_sec": gaze_valid_sec,
                        "gaze_engaged_by_role": gaze_engaged,
                        "gaze_role_score": gaze_role_score,
                        "gaze_score_for_fusion": gaze_score_for_fusion,
                        "gaze": vis_for_fusion.gaze,
                        "torso_position": vis_for_fusion.torso_position,
                        "feet_pointing": vis_for_fusion.feet_pointing,
                        "crossed_arms": vis_for_fusion.crossed_arms,
                        "sleepy_eyes": vis_for_fusion.sleepy_eyes,
                        "smile": vis_for_fusion.smile,
                        "eyebrow_raise": vis_for_fusion.eyebrow_raise,
                        "visual_ok": vis_for_fusion.ok,
                        "visual_age": visual_age,
                        "http_status": vis_for_fusion.status_code,
                        "api_latency": vis_for_fusion.latency,
                        "p_t": latest_p_t,
                        "v_t": latest_v_t,
                        "r_t": latest_r_t,
                        "s_t": latest_s_t,
                        "engagement": state.e,
                    }

                    if csv_writer is not None:
                        csv_writer.writerow(row)
                        csv_f.flush()

                    if args.print_format == "json":
                        print(json.dumps(row, ensure_ascii=False), flush=True)
                    elif args.print_format == "text":
                        print(
                            f"Frame {frame_id} | eng={state.e:.3f} | "
                            f"audio f0={fmt_float(latest_pros.f0,1)}Hz I={fmt_float(latest_pros.intensity,1)}dB "
                            f"rms={fmt_float(latest_pros.rms,4)} voiced={fmt_float(latest_pros.voiced_ratio,2)} | "
                            f"role={role_gaze.role} gaze={vis_for_fusion.gaze} "
                            f"gaze_ratio={fmt_float(gaze_ratio,2)} thr={gaze_threshold:.2f} "
                            f"gaze_score={fmt_float(gaze_score_for_fusion,2)} torso={vis_for_fusion.torso_position} "
                            f"smile={vis_for_fusion.smile} | p={latest_p_t:+.3f} v={latest_v_t:+.3f} r={latest_r_t:.2f}",
                            flush=True,
                        )

                if not args.no_window:
                    preview = frame.copy()
                    draw_overlay(
                        preview,
                        state=state,
                        pros=latest_pros,
                        vis=vis_for_fusion,
                        baseline=baseline,
                        p_t=latest_p_t,
                        v_t=latest_v_t,
                        r_t=latest_r_t,
                        calibrating=calibrating,
                        api_busy=api_busy,
                        visual_age=visual_age,
                    )
                    cv2.imshow("Realtime multimodal engagement", preview)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("q"):
                        break
                else:
                    time.sleep(0.005)

    except KeyboardInterrupt:
        print("\nInterrupted by user.", flush=True)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        print("Tip: list audio devices with `python -m sounddevice`, then pass --audio-device <INDEX>.", file=sys.stderr, flush=True)
        return 1
    finally:
        worker.stop()
        cap.release()
        cv2.destroyAllWindows()
        if csv_f is not None:
            csv_f.close()
            print(f"CSV saved to: {Path(args.out).resolve()}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
