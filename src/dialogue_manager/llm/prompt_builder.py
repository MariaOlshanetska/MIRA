from __future__ import annotations

from pathlib import Path

from dialogue_manager.llm.base import LLMRequest


PROJECT_ROOT = Path(__file__).resolve().parents[3]


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

    candidate_profession = request.state.variables.get(
        "candidate_profession",
        "the candidate's stated profession or field",
    )

    opening_delivered = bool(request.state.variables.get("opening_delivered", False))
    opening_status = (
        "Aera has already introduced herself and CCIA."
        if opening_delivered
        else "Aera has not introduced herself yet."
    )

    engagement_score = request.engagement.score
    if engagement_score is None:
        engagement_score_text = "0.500"
    else:
        engagement_score_text = f"{engagement_score:.3f}"

    return f"""
You are generating the next response in an ongoing live interview.

Session context:
- Candidate profession or field: {candidate_profession}
- Opening status: {opening_status}

Use the candidate profession or field as the concrete domain of the interview.
Do not replace it with generic examples such as “software developer” unless that is the actual profession.

Recent dialogue history, for context only:
{recent_history}

Current realtime engagement score:
{engagement_score_text}

Use the Engagement Adaptation Policy from the system prompt to interpret this score.
Do not mention the numeric engagement score to the candidate.

MOST RECENT USER INPUT:
{request.user_input.user_text}

Your highest-priority task:
Respond to the MOST RECENT USER INPUT directly.

Turn-local priorities:
* The most recent user input has priority over the interview agenda.
* Do not repeat previous greetings or introductions.
* Do not introduce yourself again after the opening.
* Do not repeat that this is a relaxed conversation after the opening.
* Do not repeat any sentence or clause from the previous Agent turn.
* Use recent history only for continuity and to avoid repetition.
* If the candidate asks “How are you?” after the opening, answer briefly and warmly, then move forward to the first interview topic. Do not introduce yourself again.
* If the candidate asks a question, answer it briefly first, then continue naturally.
* If the candidate gives a short or unclear answer, do not pressure them immediately. Try a gentler rephrasing or offer an example.
* Ask only one main question at a time.
* Follow the system prompt, response format, and annotation libraries exactly.
""".strip()
