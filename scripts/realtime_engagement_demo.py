#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realtime_engagement_demo.py

Based on realtime_multimodal_engagement.py

Run:
  python scripts/realtime_engagement_demo.py \
    --url http://10.10.200.182/engagement_maria/engagement_maria \
    --camera 0

Dependencies:
  pip install opencv-python requests pyyaml numpy sounddevice praat-parselmouth
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
from collections import deque

import cv2
import numpy as np
import requests
import sounddevice as sd
import yaml
import parselmouth


# =============================================================================
# ██████╗  ██████╗ ███╗   ██╗███████╗██╗ ██████╗ ██╗   ██╗██████╗  █████╗ ████████╗██╗ ██████╗ ███╗   ██╗
# ██╔════╝██╔═══██╗████╗  ██║██╔════╝██║██╔════╝ ██║   ██║██╔══██╗██╔══██╗╚══██╔══╝██║██╔═══██╗████╗  ██║
# ██║     ██║   ██║██╔██╗ ██║█████╗  ██║██║  ███╗██║   ██║██████╔╝███████║   ██║   ██║██║   ██║██╔██╗ ██║
# ██║     ██║   ██║██║╚██╗██║██╔══╝  ██║██║   ██║██║   ██║██╔══██╗██╔══██╗   ██║   ██║██║   ██║██║╚██╗██║
# ╚██████╗╚██████╔╝██║ ╚████║██║     ██║╚██████╔╝╚██████╔╝██║  ██║██║  ██║   ██║   ██║╚██████╔╝██║ ╚████║
#  ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝╚═╝     ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   ╚═╝ ╚═════╝ ╚═╝  ╚═══╝
#
# ALL TUNABLE PARAMETERS ARE HERE.
# =============================================================================

# -----------------------------------------------------------------------------
# CUE CONFIGURATION
# -----------------------------------------------------------------------------
# Each visual cue has:
#   "enabled":  True/False — set to False to completely ignore that cue
#   "weight":   How much this cue contributes relative to others (higher = more influence)
#   "score_positive": Evidence value when the cue indicates engagement (e.g. gaze=camera)
#   "score_negative": Evidence value when the cue indicates disengagement (e.g. gaze=away)
#
# HOW TO DISABLE A CUE:
#   Set "enabled": False — the cue will be skipped entirely in fusion.
#
# HOW TO CHANGE A CUE'S INFLUENCE:
#   - Increase "weight" to make the cue matter more in the final score.
#   - Increase abs(score_negative) to make disengagement on that cue drop faster.
#   - Increase score_positive to make engagement on that cue raise the score faster.
#
# EXAMPLE: To make crossed_arms the only thing that matters for demo:
#   Set all other cues to "weight": 0.1 and crossed_arms to "weight": 3.0
# -----------------------------------------------------------------------------

CUE_CONFIG = {
    "gaze": {
        "enabled": True,            # Whether gaze affects the score
        "weight": 1.0,              # Relative importance (1.0 = baseline)
        "score_positive": 0.30,     # Evidence when looking at camera (mild positive)
        "score_negative": -0.40,    # Evidence when looking away (moderate negative)
    },
    "torso_position": {
        "enabled": True,
        "weight": 0.8,
        "score_positive": 0.25,     # Leaning towards
        "score_negative": -0.60,    # Leaning away
    },
    "feet_pointing": {
        "enabled": True,
        "weight": 0.3,             # Feet are often unreliable / not visible
        "score_positive": 0.20,
        "score_negative": -0.40,
    },
    "crossed_arms": {
        "enabled": True,
        "weight": 2.5,             # HIGH weight — main demo trigger
        "score_positive": 0.0,     # Arms not crossed = neutral (doesn't boost)
        "score_negative": -1.8,    # Arms crossed = strong negative (drops in 3-4s)
    },
    "sleepy_eyes": {
        "enabled": True,
        "weight": 1.0,
        "score_positive": 0.0,     # Eyes open = neutral
        "score_negative": -0.80,   # Sleepy eyes = moderate negative
    },
    "smile": {
        "enabled": True,
        "weight": 0.8,
        "score_positive": 0.60,    # Smiling = positive engagement signal
        "score_negative": 0.0,     # Not smiling = neutral (not penalized)
    },
    "eyebrow_raise": {
        "enabled": True,
        "weight": 0.5,
        "score_positive": 0.50,    # Interest signal
        "score_negative": 0.0,     # Neutral brows = no effect
    },
}

# -----------------------------------------------------------------------------
# ACCUMULATOR / SMOOTHING PARAMETERS
# -----------------------------------------------------------------------------
# These control how fast the engagement score moves up and down.

ACCUMULATOR_CONFIG = {
    # STATE MEMORY (rho)
    # How much of the previous state is preserved each tick.
    # Higher = smoother/slower response. Lower = more reactive/jittery.
    # At fusion_fps=3: rho=0.985 gives a half-life of ~15 seconds.
    # Range: 0.90 (very reactive) to 0.995 (very sluggish)
    "rho": 0.985,

    # LEARNING RATES (eta_up, eta_down)
    # How fast new evidence moves the score.
    # eta_up:   speed when evidence is POSITIVE (engagement going up)
    # eta_down: speed when evidence is NEGATIVE (engagement going down)
    # For the demo: eta_down > eta_up so disengagement is visible,
    # but not 3x like before — just 1.4x for smoother behavior.
    "eta_up": 0.25,
    "eta_down": 0.35,

    # CENTER PULL
    # Slow drift toward 0.5 when no evidence is available.
    # Keep low for a 2-minute demo so score doesn't wander during pauses.
    "center_pull": 0.008,

    # HOLD ON NO EVIDENCE
    # When True: if there's no audio and no visual data, the score stays put.
    # When False: the score slowly drifts to center even with no input.
    "hold_on_no_evidence": True,
}

# -----------------------------------------------------------------------------
# PROSODY (AUDIO) CONFIGURATION
# -----------------------------------------------------------------------------
# Audio contributes to engagement when the candidate is SPEAKING.
# When silent (listener role), audio does NOT penalize — score just holds.

PROSODY_CONFIG = {
    # Overall weight of audio vs visual in the fusion.
    # w_prosody + w_visual should sum to 1.0
    "w_prosody": 0.30,             # Audio contributes 30%
    "w_visual": 0.70,              # Visual contributes 70%

    # Prosody sub-weights (how much each audio feature matters)
    # These multiply the deviation from calibration baseline:
    "w_intensity": 0.8,            # Louder than baseline = more engaged
    "w_rhythm": 0.6,               # Faster speech than baseline = more engaged
    "w_f0": 0.5,                   # Pitch variation = more expressive

    # Silence detection threshold (RMS below this = "not speaking")
    "silence_rms_thresh": 0.01,
}

# -----------------------------------------------------------------------------
# SPEECH-RATE (RHYTHM) CONFIGURATION
# -----------------------------------------------------------------------------
# "Rhythm" is estimated as the user's speaking rate in syllables per second,
# approximated directly from the acoustic signal (no ASR transcription needed)
# by intensity-peak counting -- the principle behind Praat's automatic
# syllable-nuclei detection (De Jong & Wempe, 2009).
#
# A candidate syllable nucleus is a local maximum of the intensity contour that
# (a) rises at least PEAK_PROMINENCE_DB above the surrounding intensity dips,
# and (b) coincides with voiced material. The count of accepted peaks divided by
# the voiced duration of the window gives an estimate of the syllable rate.

SYLLABLE_RATE_CONFIG = {
    # Minimum prominence (in dB) a local intensity peak must have above its
    # neighbouring dips to count as a syllable nucleus. 2 dB is the standard
    # De Jong & Wempe default; raise it (e.g. 3-4 dB) for noisier recordings.
    "peak_prominence_db": 2.0,
    # A peak must sit on voiced material: the fraction of voiced pitch frames
    # near the peak must be at least this value.
    "voiced_fraction_at_peak": 0.30,
}

# -----------------------------------------------------------------------------
# OUTPUT SMOOTHING (EMA)
# -----------------------------------------------------------------------------
# Final exponential moving average on the output score.
# This is the last layer of smoothing — removes micro-jitter from the signal.
# alpha=1.0 means no smoothing. alpha=0.3 means heavy smoothing.

OUTPUT_EMA_ALPHA = 0.4   # 0.0=frozen, 1.0=raw, 0.3-0.5=smooth for demo

# -----------------------------------------------------------------------------
# HABITUATION
# -----------------------------------------------------------------------------
# When a cue stays in the same state for a long time, its effective weight
# decays (habituation). This prevents a static posture from dominating forever.

HABITUATION_CONFIG = {
    "grace_sec": 6.0,              # Seconds before habituation starts
    "floor": 0.60,                 # Minimum effective weight (60% of original)
    # Per-cue decay rates (higher = habituates faster)
    "rates": {
        "gaze": 0.03,
        "torso_position": 0.05,
        "feet_pointing": 0.05,
        "crossed_arms": 0.04,      # Crossed arms habituates slowly (stays relevant)
        "sleepy_eyes": 0.04,
        "smile": 0.08,             # Smile habituates faster (constant smile = less signal)
        "eyebrow_raise": 0.08,
    },
}

# -----------------------------------------------------------------------------
# ROLE-AWARE GAZE
# -----------------------------------------------------------------------------
# The system tracks whether the candidate is speaking or listening.
# Gaze expectations differ by role:
#   - Speaker: doesn't need to look at camera as much (thinking, gesturing)
#   - Listener: should look at camera more (paying attention to Aera)

ROLE_GAZE_CONFIG = {
    "voiced_thresh": 0.20,         # Min voiced_ratio to count as "speaker"
    "min_speaker_sec": 0.4,        # Debounce before switching to speaker
    "min_listener_sec": 1.0,       # Debounce before switching to listener
    "speaker_gaze_window": 5.0,    # Seconds of gaze history when speaking
    "listener_gaze_window": 10.0,  # Seconds of gaze history when listening
    "min_valid_speaker_gaze": 1.5, # Min valid gaze data before affecting fusion
    "min_valid_listener_gaze": 2.0,
    "speaker_gaze_thresh": 0.30,   # Gaze ratio needed (lower = more forgiving)
    "listener_gaze_thresh": 0.45,  # Gaze ratio needed when listening
}


# =============================================================================
# END OF CONFIGURATION — Below this line is the engine. You shouldn't need to
# modify anything below unless you're changing the fusion algorithm itself.
# =============================================================================


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


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class Baseline:
    f0: float
    intensity: float
    rhythm: float


@dataclass
class ProsodyFeatures:
    f0: float
    intensity: float
    rhythm: float
    rms: float
    voiced_ratio: float


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
    z: float        # logit-space state (unbounded)
    e: float        # sigmoid(z) — the 0-1 engagement score


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

        if ratio >= threshold:
            score = (ratio - threshold) / max(1.0 - threshold, EPS)
        else:
            score = (ratio - threshold) / max(threshold, EPS)

        score = float(np.clip(score, -1.0, 1.0))
        return float(ratio), float(threshold), float(den), bool(engaged), score


# =============================================================================
# Utility functions
# =============================================================================

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
        print(f"WARNING: Could not read config ({e}), using --url or fallback only", flush=True)
        return None


def frame_to_base64(frame: np.ndarray, jpeg_quality: int = 70, max_width: int = 640) -> str:
    h, w = frame.shape[:2]
    if w > max_width:
        scale = max_width / float(w)
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
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


# =============================================================================
# Visual cue scoring — uses CUE_CONFIG from above
# =============================================================================

def score_cue(cue_name: str, label: Optional[str]) -> Optional[float]:
    """
    Score a visual cue based on CUE_CONFIG.
    Returns None if the cue is disabled or the label is unclear/not applicable.
    """
    config = CUE_CONFIG.get(cue_name)
    if config is None or not config["enabled"]:
        return None

    lab = normalize_label(label)

    if cue_name == "gaze":
        if lab == "camera":
            return config["score_positive"]
        if lab == "away":
            return config["score_negative"]
        return None

    if cue_name == "torso_position":
        if lab == "towards":
            return config["score_positive"]
        if lab == "away":
            return config["score_negative"]
        return None

    if cue_name == "feet_pointing":
        if lab == "towards":
            return config["score_positive"]
        if lab == "away":
            return config["score_negative"]
        # "not_visible" -> None (ignored)
        return None

    if cue_name == "crossed_arms":
        if lab == "yes":
            return config["score_negative"]
        if lab == "no":
            return config["score_positive"]
        return None

    if cue_name == "sleepy_eyes":
        if lab == "yes":
            return config["score_negative"]
        if lab == "no":
            return config["score_positive"]
        return None

    if cue_name == "smile":
        if lab == "yes":
            return config["score_positive"]
        if lab == "no":
            return config["score_negative"]
        return None

    if cue_name == "eyebrow_raise":
        if lab == "yes":
            return config["score_positive"]
        if lab == "no":
            return config["score_negative"]
        return None

    return None


# =============================================================================
# Engagement Accumulator (fusion engine)
# =============================================================================

class EngagementAccumulator:
    """
    Logit-space engagement accumulator with configurable cue weights.

    The formula each tick:
        z_new = rho * z_old + eta * reliability * evidence - center_pull * tanh(z_old)
        engagement = sigmoid(z_new)
    """

    def __init__(self):
        # Load from config dicts defined at top of file
        self.rho = ACCUMULATOR_CONFIG["rho"]
        self.eta_up = ACCUMULATOR_CONFIG["eta_up"]
        self.eta_down = ACCUMULATOR_CONFIG["eta_down"]
        self.center_pull = ACCUMULATOR_CONFIG["center_pull"]
        self.hold_on_no_evidence = ACCUMULATOR_CONFIG["hold_on_no_evidence"]

        self.w_prosody = PROSODY_CONFIG["w_prosody"]
        self.w_visual = PROSODY_CONFIG["w_visual"]
        self.a0 = PROSODY_CONFIG["w_intensity"]
        self.a1 = PROSODY_CONFIG["w_rhythm"]
        self.a2 = PROSODY_CONFIG["w_f0"]
        self.silence_rms_thresh = PROSODY_CONFIG["silence_rms_thresh"]

        self.habituation_grace_sec = HABITUATION_CONFIG["grace_sec"]
        self.habituation_floor = HABITUATION_CONFIG["floor"]
        self.habituation_rates = HABITUATION_CONFIG["rates"]

        # Internal state for habituation tracking
        self._visual_prev = {field: None for field in FIELD_ORDER}
        self._visual_duration_sec = {field: 0.0 for field in FIELD_ORDER}

    def observe_visual(self, vis: VisualFeatures, dt_sec: float) -> None:
        """Track how long each visual label has been constant (for habituation)."""
        dt_sec = max(float(dt_sec), 0.0)
        for cue_name in FIELD_ORDER:
            lab = normalize_label(getattr(vis, cue_name))
            if lab is None or lab == "unclear" or (cue_name == "feet_pointing" and lab == "not_visible"):
                self._visual_prev[cue_name] = None
                self._visual_duration_sec[cue_name] = 0.0
                continue
            if self._visual_prev[cue_name] == lab:
                self._visual_duration_sec[cue_name] += dt_sec
            else:
                self._visual_prev[cue_name] = lab
                self._visual_duration_sec[cue_name] = dt_sec

    def _effective_weight(self, cue_name: str, base_weight: float, score: Optional[float]) -> float:
        """Apply habituation decay to a cue's weight."""
        if base_weight <= 0.0 or score is None or abs(score) <= EPS:
            return base_weight
        rate = self.habituation_rates.get(cue_name, 0.0)
        if rate <= 0.0:
            return base_weight
        duration_sec = self._visual_duration_sec.get(cue_name, 0.0)
        effective_duration = max(0.0, duration_sec - self.habituation_grace_sec)
        decay = float(np.exp(-rate * effective_duration))
        decay = max(self.habituation_floor, decay)
        return base_weight * decay

    def prosody_active(self, pros: ProsodyFeatures) -> bool:
        return pros.rms >= self.silence_rms_thresh

    def prosody_reliability(self, pros: ProsodyFeatures) -> float:
        if not self.prosody_active(pros):
            return 0.0
        r_rms = float(np.clip(pros.rms / (self.silence_rms_thresh + EPS), 0.0, 1.0))
        r_voiced = float(np.clip(pros.voiced_ratio / 0.5, 0.0, 1.0))
        return float(0.7 * r_rms + 0.3 * r_voiced)

    def visual_evidence(
        self,
        vis: VisualFeatures,
        gaze_score_override: Optional[float] = None,
        use_gaze_override: bool = False,
    ) -> Tuple[float, float]:
        """
        Compute visual evidence score and availability ratio.
        Returns (v_t, availability_ratio).
        """
        if not vis.ok:
            return 0.0, 0.0

        pairs = []
        available_weight = 0.0
        possible_weight = 0.0

        for cue_name in FIELD_ORDER:
            config = CUE_CONFIG.get(cue_name)
            if config is None or not config["enabled"]:
                continue

            weight = config["weight"]
            if weight <= 0:
                continue

            label = getattr(vis, cue_name)
            lab = normalize_label(label)

            # Skip non-informative labels
            if lab == "unclear" or (cue_name == "feet_pointing" and lab == "not_visible"):
                continue

            possible_weight += weight

            # Use gaze override from role-gaze tracker if available
            if cue_name == "gaze" and use_gaze_override and gaze_score_override is not None:
                score = gaze_score_override
            else:
                score = score_cue(cue_name, label)

            if score is None:
                continue

            available_weight += weight
            effective_weight = self._effective_weight(cue_name, weight, score)
            pairs.append((effective_weight, score))

        if not pairs:
            return 0.0, 0.0

        weighted_sum = float(sum(w * s for w, s in pairs))
        # Normalize by available weight so the scale stays -1 to +1 ish
        v_t = weighted_sum / max(available_weight, EPS)
        avail_ratio = float(np.clip(available_weight / max(possible_weight, EPS), 0.0, 1.0))
        return float(v_t), avail_ratio

    def reliability(
        self,
        vis: VisualFeatures,
        pros: ProsodyFeatures,
        gaze_score_override: Optional[float] = None,
        use_gaze_override: bool = False,
    ) -> float:
        """Combined reliability from prosody + visual channels."""
        active_reliabilities = []
        r_pro = self.prosody_reliability(pros)
        if r_pro > 0.0:
            active_reliabilities.append(r_pro)
        _, r_vis = self.visual_evidence(
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
        """
        Compute fused evidence score.
        Returns (s_t, p_t, v_t).

        KEY CHANGE from original: when prosody is NOT active (silence),
        p_t = 0 (neutral). This means silence does NOT hurt the score.
        The score only drops from visual cues showing disengagement.
        """
        if self.prosody_active(pros):
            # Prosody deviation from baseline: positive means more animated/engaged
            p_t = self.a0 * dI + self.a1 * dR + self.a2 * abs(dF0)
        else:
            # SILENCE = NEUTRAL, not negative. Listener might be speaking.
            p_t = 0.0

        v_t, _ = self.visual_evidence(
            vis,
            gaze_score_override=gaze_score_override,
            use_gaze_override=use_gaze_override,
        )

        # Weighted fusion of prosody and visual evidence
        s_t = self.w_prosody * p_t + self.w_visual * v_t
        return float(s_t), float(p_t), float(v_t)

    def update(self, state: EngagementState, r_t: float, s_t: float) -> EngagementState:
        """Update the engagement state with new evidence."""
        pull_to_center = self.center_pull * np.tanh(state.z)

        # If no evidence available, hold the score (just apply tiny center pull)
        if self.hold_on_no_evidence and (r_t <= 0.0 or abs(s_t) <= EPS):
            z_new = state.z - pull_to_center
            return EngagementState(z=float(z_new), e=sigmoid(z_new))

        # Asymmetric learning rate: negative evidence moves slightly faster
        eta = self.eta_down if s_t < 0.0 else self.eta_up

        z_new = self.rho * state.z + eta * r_t * s_t - pull_to_center
        return EngagementState(z=float(z_new), e=sigmoid(z_new))


# =============================================================================
# Audio ring buffer and prosody analysis
# =============================================================================

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
        ring.add(indata[:, 0])
    return callback


def coerce_audio_device(value):
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


def estimate_syllable_rate(
    intensity_values: np.ndarray,
    voiced_mask: np.ndarray,
    time_step: float,
    peak_prominence_db: float,
    voiced_fraction_at_peak: float,
) -> float:
    """
    Estimate speaking rate in syllables per second by intensity-peak counting.

    This follows the principle of Praat's automatic syllable-nuclei detection
    (De Jong & Wempe, 2009): a syllable nucleus is a local maximum of the
    intensity contour that (a) rises at least ``peak_prominence_db`` above the
    surrounding intensity dips, and (b) sits on voiced material. The number of
    accepted peaks divided by the voiced duration of the window gives the rate.

    Args:
        intensity_values: intensity contour (dB), one value per analysis frame.
        voiced_mask: boolean array (same frame grid as pitch) marking voiced frames.
        time_step: seconds between consecutive intensity frames.
        peak_prominence_db: minimum dip-to-peak rise for a valid nucleus.
        voiced_fraction_at_peak: minimum voiced fraction in a small window
            around the peak for it to count.

    Returns:
        Estimated syllable rate in syllables per second (>= 0.0).
    """
    n = intensity_values.size
    if n < 3 or time_step <= 0.0:
        return 0.0

    # Voiced duration of this window, used as the denominator. Pitch and
    # intensity are sampled on the same 10 ms grid, so we can reuse voiced_mask.
    voiced_frames = int(np.count_nonzero(voiced_mask)) if voiced_mask.size else 0
    voiced_duration = voiced_frames * time_step
    if voiced_duration <= 0.0:
        return 0.0

    # Map an intensity-frame index onto the voiced mask (guard length mismatch).
    def is_peak_voiced(idx: int) -> bool:
        if voiced_mask.size == 0:
            return True
        # small +/- 2 frame window around the peak
        lo = max(0, idx - 2)
        hi = min(voiced_mask.size, idx + 3)
        window = voiced_mask[lo:hi]
        if window.size == 0:
            return False
        return float(np.mean(window)) >= voiced_fraction_at_peak

    peaks = 0
    # A local maximum: strictly greater than immediate neighbours.
    for i in range(1, n - 1):
        if not (intensity_values[i] > intensity_values[i - 1]
                and intensity_values[i] >= intensity_values[i + 1]):
            continue

        # Nearest dip to the left: scan back until intensity rises again.
        left_min = intensity_values[i]
        j = i - 1
        while j >= 0 and intensity_values[j] <= left_min:
            left_min = intensity_values[j]
            j -= 1

        # Nearest dip to the right.
        right_min = intensity_values[i]
        k = i + 1
        while k < n and intensity_values[k] <= right_min:
            right_min = intensity_values[k]
            k += 1

        # Prominence = rise above the higher of the two neighbouring dips.
        dip = max(left_min, right_min)
        prominence = intensity_values[i] - dip
        if prominence < peak_prominence_db:
            continue

        if not is_peak_voiced(i):
            continue

        peaks += 1

    return float(peaks) / float(voiced_duration)


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
        intensity_finite = intensity_values[np.isfinite(intensity_values)]
        intensity_mean = float(np.mean(intensity_finite)) if intensity_finite.size else 0.0

        # For peak counting, replace non-finite frames (edge padding) with a low
        # sentinel so they act as dips and never register as syllable nuclei.
        intensity_clean = np.where(
            np.isfinite(intensity_values),
            intensity_values,
            (float(np.min(intensity_finite)) - 1.0) if intensity_finite.size else 0.0,
        )

        # "Rhythm" is the speaking rate in syllables per second, approximated by
        # intensity-peak counting on the same 10 ms frame grid used for pitch.
        # Align the voiced mask to the intensity contour (both ~10 ms frames);
        # lengths can differ by a frame or two, so trim to the common length.
        common = min(intensity_clean.size, voiced.size)
        rhythm = estimate_syllable_rate(
            intensity_values=intensity_clean[:common],
            voiced_mask=voiced[:common],
            time_step=0.01,
            peak_prominence_db=SYLLABLE_RATE_CONFIG["peak_prominence_db"],
            voiced_fraction_at_peak=SYLLABLE_RATE_CONFIG["voiced_fraction_at_peak"],
        )

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
            seg, sr,
            pitch_floor=pitch_floor,
            pitch_ceiling=pitch_ceiling,
            silence_rms_thresh=silence_rms_thresh,
        )
        if pros.rms > silence_rms_thresh and pros.voiced_ratio > 0.2:
            if np.isfinite(pros.f0):
                f0_vals.append(pros.f0)
            if np.isfinite(pros.intensity) and pros.intensity > 0.0:
                int_vals.append(pros.intensity)
            # rhythm is now syllables/second; only count windows where a
            # non-zero rate was actually detected, so a few flat windows do
            # not pull the baseline rate down toward zero.
            if np.isfinite(pros.rhythm) and pros.rhythm > 0.0:
                rhy_vals.append(pros.rhythm)

    if not f0_vals:
        f0_vals = [180.0]
    if not int_vals:
        int_vals = [55.0]
    if not rhy_vals:
        # Fallback baseline speaking rate in syllables/second, roughly the
        # typical conversational rate, used only if calibration found no voiced
        # speech. The rhythm field now stores syllable rate, not intensity std.
        rhy_vals = [4.0]

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


def safe_relative_delta(x: float, base: float) -> float:
    """Relative deviation (x - base) / base, robust to invalid inputs.

    Used for the syllable-rate (rhythm) feature, which is a positive rate in
    syllables/second rather than a dB-scale quantity. A value above zero means
    the user is speaking faster than their calibrated baseline.
    """
    if not np.isfinite(x) or not np.isfinite(base) or base <= EPS:
        return 0.0
    return float((x - base) / base)


def prosody_deltas(pros: ProsodyFeatures, baseline: Baseline) -> Tuple[float, float, float]:
    dF0 = safe_log_ratio(pros.f0, baseline.f0)
    dI = safe_db_delta(pros.intensity, baseline.intensity, scale_db=10.0)
    # Rhythm is now a syllable rate, so its deviation is measured relative to
    # the speaker's baseline rate rather than on a dB scale.
    dR = safe_relative_delta(pros.rhythm, baseline.rhythm)
    return dF0, dI, dR


# =============================================================================
# VLM API parsing
# =============================================================================

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
    for cue_name in FIELD_ORDER:
        lab = normalize_label_value(d.get(cue_name))
        if lab in ALLOWED_LABELS[cue_name]:
            values[cue_name] = lab
            any_valid = True
        else:
            values[cue_name] = None

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


# =============================================================================
# API worker thread
# =============================================================================

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


# =============================================================================
# OpenCV overlay
# =============================================================================

def draw_overlay(
    frame: np.ndarray,
    state: EngagementState,
    smoothed_e: float,
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
    """Compact top-left overlay showing engagement score and cue state."""
    overlay = frame.copy()
    h, w = overlay.shape[:2]

    box_w = min(380, w - 12)
    box_h = 140
    x0, y0 = 6, 4
    x1, y1 = x0 + box_w, y0 + box_h

    cv2.rectangle(overlay, (x0, y0), (x1, y1), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.52, frame, 0.48, 0, frame)

    e = smoothed_e
    if e >= 0.66:
        color = (40, 220, 40)       # Green = engaged
    elif e <= 0.33:
        color = (40, 40, 220)       # Red = disengaged
    else:
        color = (0, 220, 220)       # Yellow = neutral

    status = "CALIBRATING" if calibrating else "RUNNING"
    lat = "nan" if vis.latency is None else f"{vis.latency:.2f}s"

    y = y0 + 25
    cv2.putText(frame, f"Engagement: {e:.3f}", (x0 + 10, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.68, color, 2, cv2.LINE_AA)

    y += 22
    cv2.putText(frame, f"{status} | API busy={api_busy} | age={visual_age:.1f}s", (x0 + 10, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.43, (235, 235, 235), 1, cv2.LINE_AA)

    y += 20
    cv2.putText(frame, f"gaze={vis.gaze}  torso={vis.torso_position}  feet={vis.feet_pointing}", (x0 + 10, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (235, 235, 235), 1, cv2.LINE_AA)

    y += 20
    cv2.putText(frame, f"arms={vis.crossed_arms}  sleepy={vis.sleepy_eyes}  smile={vis.smile}", (x0 + 10, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (235, 235, 235), 1, cv2.LINE_AA)

    y += 20
    cv2.putText(frame, f"p={p_t:+.2f} v={v_t:+.2f} r={r_t:.2f} | lat={lat}", (x0 + 10, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.40, (220, 220, 220), 1, cv2.LINE_AA)

    # Engagement bar
    bar_x0, bar_y0 = x0 + 10, y1 - 13
    bar_x1 = x1 - 10
    cv2.rectangle(frame, (bar_x0, bar_y0), (bar_x1, bar_y0 + 6), (70, 70, 70), -1)
    cv2.rectangle(frame, (bar_x0, bar_y0),
                  (bar_x0 + int((bar_x1 - bar_x0) * e), bar_y0 + 6), color, -1)
    return frame


# =============================================================================
# CSV logging
# =============================================================================

def open_csv_writer(path: Optional[str]):
    if not path:
        return None, None
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(out_path, "w", newline="", encoding="utf-8")
    fieldnames = [
        "wall_time", "frame_id",
        "f0", "intensity_db", "syllable_rate", "rms", "voiced_ratio",
        "dF0", "dI", "dR",
        "role", "raw_role",
        "gaze", "gaze_ratio_role_window", "gaze_threshold",
        "gaze_valid_sec", "gaze_engaged_by_role", "gaze_role_score", "gaze_score_for_fusion",
        "torso_position", "feet_pointing", "crossed_arms",
        "sleepy_eyes", "smile", "eyebrow_raise",
        "visual_ok", "visual_age", "http_status", "api_latency",
        "p_t", "v_t", "r_t", "s_t",
        "engagement_raw", "engagement",
    ]
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    return f, writer


# =============================================================================
# CLI arguments (only infrastructure params — fusion params come from config above)
# =============================================================================

def parse_args():
    default_url = load_config_url()
    parser = argparse.ArgumentParser(
        description="Realtime engagement recognition (demo-tuned)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
TUNING:
  All engagement fusion parameters are at the TOP of this script file.
  Edit CUE_CONFIG, ACCUMULATOR_CONFIG, PROSODY_CONFIG, etc. directly.
  CLI args here are only for infrastructure (camera, audio device, API URL).
""",
    )
    parser.add_argument("--url", type=str, default=default_url, help="VLM Engagement API endpoint URL")
    parser.add_argument("--dataset", type=str, default="debugging", choices=["therapy", "debugging"])
    parser.add_argument("--verbose", action="store_true", help="Ask API for verbose output")
    parser.add_argument("--quality", type=int, default=70, help="JPEG quality for API frames")
    parser.add_argument("--fps", type=float, default=3.0, help="Frames per second sent to the API")
    parser.add_argument("--fusion-fps", type=float, default=3.0, help="Engagement fusion updates per second")
    parser.add_argument("--timeout", type=float, default=10.0, help="API request timeout")
    parser.add_argument("--camera", type=int, default=0, help="OpenCV camera index")
    parser.add_argument("--max-width", type=int, default=640, help="Resize frames before API call")
    parser.add_argument("--max-visual-age", type=float, default=2.5, help="Ignore stale visual results (seconds)")

    parser.add_argument("--sr", type=int, default=44100, help="Microphone sample rate")
    parser.add_argument("--audio-device", default=None, help="sounddevice input device; run `python -m sounddevice` to list")
    parser.add_argument("--audio-window", type=float, default=1.0, help="Seconds of audio per prosody estimate")
    parser.add_argument("--ring-seconds", type=float, default=12.0, help="Audio ring buffer length")
    parser.add_argument("--calib", type=float, default=5.0, help="Initial microphone calibration seconds")
    parser.add_argument("--pitch-floor", type=float, default=75.0)
    parser.add_argument("--pitch-ceiling", type=float, default=500.0)
    parser.add_argument("--audio-debug", action="store_true", help="Print live mic RMS/peak once per second")

    parser.add_argument("--out", type=str, default="realtime_engagement_demo_log.csv", help="CSV log path")
    parser.add_argument("--print-format", choices=["text", "json", "none"], default="text")
    parser.add_argument("--no-window", action="store_true", help="Do not open OpenCV window")

    return parser.parse_args()


# =============================================================================
# Main loop
# =============================================================================

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
    model = EngagementAccumulator()
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

    # --- State variables ---
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

    # Output EMA state
    smoothed_engagement = 0.5

    rcfg = ROLE_GAZE_CONFIG  # shorthand

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

                # Audio debug
                if args.audio_debug and now - last_audio_debug >= 1.0:
                    last_audio_debug = now
                    y_dbg = ring.get_last(min(1.0, args.audio_window))
                    rms_dbg, peak_dbg = audio_level(y_dbg)
                    print(
                        f"AUDIO_DEBUG device={audio_device!r} sr={args.sr} "
                        f"samples={ring.total_samples} rms={rms_dbg:.6f} peak={peak_dbg:.6f}",
                        flush=True,
                    )

                # Submit frame to API worker
                if now - last_api_submit >= 1.0 / max(float(args.fps), EPS):
                    if worker.submit_latest(frame_id, frame):
                        last_api_submit = now

                # Get latest visual from API
                vis, latest_visual_frame_id, api_busy = worker.get_latest()
                visual_age = now - vis.timestamp if vis.timestamp > 0 else 999.0
                if visual_age > args.max_visual_age:
                    vis_for_fusion = VisualFeatures(
                        None, None, None, None, None, None, None,
                        raw="STALE_VISUAL", ok=False,
                        timestamp=vis.timestamp, status_code=vis.status_code, latency=vis.latency,
                    )
                else:
                    vis_for_fusion = vis

                # Calibration
                calibrating = baseline is None
                if baseline is None and elapsed >= args.calib and ring.ready(args.calib):
                    calib_audio = ring.get_last(args.calib)
                    if calib_audio is not None:
                        baseline = calibrate_baseline_from_audio(
                            calib_audio, args.sr,
                            silence_rms_thresh=PROSODY_CONFIG["silence_rms_thresh"],
                            pitch_floor=args.pitch_floor,
                            pitch_ceiling=args.pitch_ceiling,
                        )
                        print(
                            f"Baseline calibrated: f0={baseline.f0:.1f}Hz "
                            f"intensity={baseline.intensity:.1f}dB rate={baseline.rhythm:.2f}syll/s",
                            flush=True,
                        )
                        calibrating = False

                # --- Fusion tick ---
                if baseline is not None and now - last_fusion >= last_fusion_dt:
                    dt = now - last_fusion if last_fusion > 0 else last_fusion_dt
                    last_fusion = now

                    # Defaults
                    s_t = 0.0
                    raw_role = role_gaze.role
                    dF0 = dI = dR = 0.0
                    gaze_ratio = np.nan
                    gaze_threshold = np.nan
                    gaze_valid_sec = 0.0
                    gaze_engaged = None
                    gaze_role_score = 0.0
                    gaze_score_for_fusion = None

                    # Prosody analysis
                    y = ring.get_last(args.audio_window)
                    latest_pros = analyze_prosody_parselmouth(
                        y, args.sr,
                        pitch_floor=args.pitch_floor,
                        pitch_ceiling=args.pitch_ceiling,
                        silence_rms_thresh=PROSODY_CONFIG["silence_rms_thresh"],
                    )
                    dF0, dI, dR = prosody_deltas(latest_pros, baseline)

                    # Role detection
                    raw_role = role_gaze.raw_role_from_prosody(
                        latest_pros,
                        silence_rms_thresh=PROSODY_CONFIG["silence_rms_thresh"],
                        voiced_thresh=rcfg["voiced_thresh"],
                    )
                    role_gaze.update_role(
                        raw_role=raw_role,
                        now=now,
                        min_speaker_sec=rcfg["min_speaker_sec"],
                        min_listener_sec=rcfg["min_listener_sec"],
                    )

                    # Gaze tracking
                    role_gaze.add_gaze_sample(vis=vis_for_fusion, now=now, dt=dt)

                    gaze_ratio, gaze_threshold, gaze_valid_sec, gaze_engaged, gaze_role_score = (
                        role_gaze.gaze_ratio_for_current_role(
                            now=now,
                            speaker_window=rcfg["speaker_gaze_window"],
                            listener_window=rcfg["listener_gaze_window"],
                            min_valid_speaker=rcfg["min_valid_speaker_gaze"],
                            min_valid_listener=rcfg["min_valid_listener_gaze"],
                            speaker_threshold=rcfg["speaker_gaze_thresh"],
                            listener_threshold=rcfg["listener_gaze_thresh"],
                        )
                    )

                    # Convert role-gaze score to fusion-compatible value
                    if gaze_engaged is None:
                        gaze_score_for_fusion = None
                    else:
                        if role_gaze.role == "listener":
                            gaze_score_for_fusion = 0.45 * max(gaze_role_score, 0.0) + 0.45 * min(gaze_role_score, 0.0)
                        else:
                            gaze_score_for_fusion = 0.55 * max(gaze_role_score, 0.0) + 0.45 * min(gaze_role_score, 0.0)

                    # NOTE: Removed the "instant gaze-away override" from the original
                    # script. That was causing single-frame spikes. The role-gaze window
                    # handles gaze smoothly over time now.

                    # Habituation tracking
                    model.observe_visual(vis_for_fusion, dt_sec=dt)

                    # Reliability
                    latest_r_t = model.reliability(
                        vis=vis_for_fusion,
                        pros=latest_pros,
                        gaze_score_override=gaze_score_for_fusion,
                        use_gaze_override=True,
                    )

                    # Evidence
                    s_t, latest_p_t, latest_v_t = model.evidence_score(
                        dF0=dF0, dI=dI, dR=dR,
                        pros=latest_pros,
                        vis=vis_for_fusion,
                        gaze_score_override=gaze_score_for_fusion,
                        use_gaze_override=True,
                    )
                    latest_s_t = float(s_t)

                    # State update
                    state = model.update(state, r_t=latest_r_t, s_t=latest_s_t)

                    # Output EMA smoothing (final layer)
                    smoothed_engagement = (
                        OUTPUT_EMA_ALPHA * state.e
                        + (1.0 - OUTPUT_EMA_ALPHA) * smoothed_engagement
                    )

                    # Baseline drift
                    baseline = baseline_drift_update(
                        baseline, latest_pros,
                        lam=0.01,
                        rms_thresh=PROSODY_CONFIG["silence_rms_thresh"],
                    )

                    # --- Logging ---
                    row = {
                        "wall_time": round(now, 3),
                        "frame_id": frame_id,
                        "f0": latest_pros.f0,
                        "intensity_db": latest_pros.intensity,
                        "syllable_rate": latest_pros.rhythm,
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
                        "engagement_raw": state.e,
                        "engagement": smoothed_engagement,
                    }

                    if csv_writer is not None:
                        csv_writer.writerow(row)
                        csv_f.flush()

                    if args.print_format == "json":
                        # The dialogue manager reads "engagement" from JSON output
                        print(json.dumps(row, ensure_ascii=False), flush=True)
                    elif args.print_format == "text":
                        print(
                            f"Frame {frame_id} | eng={smoothed_engagement:.3f} (raw={state.e:.3f}) | "
                            f"role={role_gaze.role} gaze={vis_for_fusion.gaze} "
                            f"arms={vis_for_fusion.crossed_arms} smile={vis_for_fusion.smile} | "
                            f"p={latest_p_t:+.3f} v={latest_v_t:+.3f} r={latest_r_t:.2f}",
                            flush=True,
                        )

                # --- Display ---
                if not args.no_window:
                    preview = frame.copy()
                    draw_overlay(
                        preview,
                        state=state,
                        smoothed_e=smoothed_engagement,
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
                    cv2.imshow("Engagement Demo", preview)
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
        import traceback
        traceback.print_exc()
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
