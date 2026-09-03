# MIRA

**M**ultimodal **I**nteractive **R**esponsive **A**gent — an LLM-based embodied conversational agent that perceives the user through voice and camera, estimates their interaction state (*engagement*), and responds with expressive speech and (work in progress) gestures on a MetaHuman in Unreal Engine.

This repository contains the code for Maria Olshanetska's Master's Thesis (TFM) in Cognitive Science.

> **Project status.** The full speech pathway (listen → transcribe → estimate engagement → generate response → speak with expressive voice) works end to end. Real-time rendering of gestures and facial expressions on the MetaHuman is ongoing work: the annotations are generated, parsed, and logged, but not yet rendered in real time.

---

## What it does

The system reproduces the functional organisation of a situated dialogue, in a continuous loop:

```
perceive  →  maintain a state  →  adapt  →  act (voice + body)
```

- **Perceive.** Transcribes the user's speech and, in parallel, extracts prosodic cues (microphone) and visual behaviour cues (camera).
- **State.** Integrates those cues over time to estimate an *engagement* value between 0 and 1.
- **Adapt.** The state modulates the dialogue strategy (for example, repairing the conversation when engagement drops, or elaborating when it holds).
- **Act.** An LLM generates the reply, which is split into a speech channel (expressive TTS with emotion, pauses, and vocal bursts) and an embodiment channel (gestures and facial expressions for the MetaHuman).

The demonstration scenario is a job interview with the agent **Aera**.

---

## Requirements

- **Python 3.10 or higher**
- A **webcam** and a **microphone** (for real-time perception)
- Access to the external services used by the prototype:
  - A **Qwen model** served through an OpenAI-compatible API
  - An **engagement estimation service** (HTTP endpoint)
  - An **expressive TTS service**

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/MariaOlshanetska/MIRA.git
cd MIRA

# 2. (Recommended) create a virtual environment
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# 3. Install the package and its dependencies
pip install -e .

# For development (tests, linter, type-checking):
pip install -e ".[dev]"
```

### Configuration

Copy the example file and adjust the values to your environment:

```bash
cp .env.example .env
```

`.env` defines how the system connects to the dialogue model:

| Variable | Description |
|---|---|
| `QWEN_API_BASE_URL` | Base URL of the OpenAI-compatible API serving the model |
| `QWEN_MODEL` | Name of the model to use |
| `QWEN_MAX_TOKENS` | Maximum number of tokens in the response |
| `QWEN_TEMPERATURE` | Sampling temperature |
| `QWEN_TIMEOUT_SECONDS` | Maximum wait time per request |

### Whisper model (transcription)

Download the local Whisper model before first use:

```bash
python scripts/download_whisper_model.py
```

---

## How to run it

The system has two runnable pieces that work together.

### 1. Engagement recognizer (camera + microphone)

Analyses the camera and microphone in real time and publishes the engagement value. This is where, for now, all the perception and cue-fusion logic lives.

```bash
python scripts/realtime_engagement_demo.py \
  --url http://<host>/engagement_maria/engagement_maria \
  --camera 0
```

### 2. Voice dialogue pipeline

Listens to the user, transcribes, queries the engagement state, generates the reply with Qwen, and plays it back with expressive voice.

```bash
python scripts/run_voice_pipeline_qwen_engagement.py
```

> The dialogue pipeline reads the most recent engagement value produced by the recognizer before building each reply.

---

## Project structure

The code is written in **Python** and organised as a package, `dialogue_manager`, whose modules follow the flow of a conversational turn:

```
src/dialogue_manager/
├── core/         Turn pipeline that coordinates the interaction
├── stt/          Speech capture and transcription (Whisper)
├── engagement/   Interface connecting the recognizer to the dialogue
├── llm/          Prompt building and model querying (Qwen)
├── output/       Parsing the reply into speech and embodiment channels
├── tts/          Expressive voice realisation
├── unreal/       Sending embodiment commands to the MetaHuman
├── utils/        Logging, error handling
└── config.py     Shared configuration and settings

scripts/          Runnable entry points and utilities
configs/          Configuration and agent prompt files
  └── prompts/    Aera's scenario and behaviour repertoire
data/bursts/      Vocal bursts (laugh, sigh, surprise, etc.)
```

> **Note on the architecture.** The structure above reflects the intended design. In its current state, all real-time perception (extracting cues from camera and microphone, and fusing them into an engagement value) lives provisionally in the script `scripts/realtime_engagement_demo.py`. The `engagement/` module currently holds only the interface that connects that recognizer to the dialogue manager. Consolidating this logic into the module is pending work.

The prompt files (agent scenario and behaviour) are kept separate from the code, so the interaction can be re-authored without touching the implementation.

---

## Technologies

| Function | Tool |
|---|---|
| Speech transcription (ASR) | Whisper (faster-whisper) |
| Prosodic analysis | Parselmouth / Praat |
| Visual behaviour | Vision-language model (VLM) |
| Dialogue management | Qwen (served via an OpenAI-compatible API) |
| Expressive voice (TTS) | Expressive TTS service |
| Embodiment | MetaHuman in Unreal Engine (work in progress) |

Development was carried out with the assistance of [Kiro](https://kiro.dev), from precise instructions defining the intended behaviour of each component. All generated code was reviewed, tested, and adapted to the intended design.

---

## Development

```bash
pytest        # run the tests
ruff check .  # linter
mypy src      # type-checking
```

---

## Author

Maria Olshanetska — Master's Thesis in Cognitive Science.
