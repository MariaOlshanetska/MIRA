from __future__ import annotations

from pathlib import Path

from dialogue_manager.llm.base import LLMRequest


PROJECT_ROOT = Path.cwd()


def load_prompt_file(relative_path: str) -> str:
    path = PROJECT_ROOT / relative_path

    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    return path.read_text(encoding="utf-8").strip()


def load_optional_prompt_file(relative_path: str) -> str:
    path = PROJECT_ROOT / relative_path

    if not path.exists():
        return ""

    return path.read_text(encoding="utf-8").strip()


def build_system_prompt() -> str:
    system_prompt = load_prompt_file("configs/prompts/system_dialogue_manager.md")
    response_format = load_prompt_file("configs/prompts/response_format.md")
    tts_bursts = load_optional_prompt_file("configs/prompts/tts_bursts.md")
    gesture_library = load_optional_prompt_file("configs/prompts/unreal_gesture_library.md")

    parts = [
        system_prompt,
        response_format,
    ]

    if tts_bursts:
        parts.append(tts_bursts)

    if gesture_library:
        parts.append(gesture_library)

    return "\n\n".join(parts)


def build_user_prompt(request: LLMRequest) -> str:
    recent_history = request.state.recent_history_as_text(limit=4)

    if not recent_history:
        recent_history = "[No previous dialogue history.]"

    engagement_summary = request.engagement.summary or "[No engagement summary available.]"

    return f"""
You are generating the next response in an ongoing live interview.

Recent dialogue history, for context only:
{recent_history}

Current engagement:
- level: {request.engagement.level}
- score: {request.engagement.score}
- summary: {engagement_summary}

MOST RECENT USER INPUT:
{request.user_input.user_text}

Your highest-priority task:
Respond to the MOST RECENT USER INPUT directly.

Dialogue rules:
- The most recent user input has priority over the interview agenda.
- Use the dialogue history only as background context.
- Do not repeat previous greetings.
- Do not restart the interview.
- Do not keep asking the same question if the candidate already answered it.
- If the candidate asks a question, answer it briefly first.
- If the candidate gives an answer, acknowledge it briefly before moving on.
- If engagement is low, do not just repeat the interview question. Make an interaction-repair move.
- If engagement is very low, you may ask whether the candidate wants to continue or finish the interview.
- Ask only one question at a time.
- Keep the response concise and natural.

Annotation rules:
- output only one annotated response
- use square brackets [] for TTS annotations
- use asterisks *channel: name* for Unreal annotations
- use only Unreal annotation names listed in the Unreal gesture library
- do not use generic face names such as smile or attentive; use exact asset names such as FACE_SMILE_01
- do not invent TTS burst names
- do not output JSON
- do not explain anything
- use [emotion: happiness], [emotion: angry], [emotion: sad], [emotion: neutre], or [emotion: whisper] for TTS emotion
- use [silence: seconds] for TTS silences
- place every TTS, face and gesture annotation immediately BEFORE the words it should affect
- never place a gesture annotation after the sentence it is meant to accompany
- use [burst: burst_name] only when clearly appropriate
""".strip()