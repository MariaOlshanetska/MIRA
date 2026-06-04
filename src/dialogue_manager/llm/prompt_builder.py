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

    engagement_score = request.engagement.score
    if engagement_score is None:
        engagement_score_text = "0.500"
    else:
        engagement_score_text = f"{engagement_score:.3f}"

    return f"""
You are generating the next response in an ongoing live interview.

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

Dialogue style and continuity rules:

* The most recent user input has priority over the interview agenda.
* Sound like a real person in a live conversation, not like a questionnaire or a dialogue system.
* React to the candidate’s actual words before moving on. Use specific references to what they just said instead of generic acknowledgements.
* Prefer natural transitions such as “Right, I see”, “That makes sense”, “Oh, interesting”, “I like that”, “Okay, let me ask it this way”, or “That is a good point”.
* Do not use the same response pattern every turn. Avoid always doing: acknowledgement + follow-up question.
* It is okay to use small human-like hesitation or thinking moves when natural, such as “Hmm”, “Let me think”, “Actually”, “I mean”, or “Maybe I can ask that differently”.
* Keep the conversation warm and lightly informal, while still sounding professional.
* Do not over-explain the interview process unless the candidate seems confused.
* Do not repeat previous greetings.
* Do not restart the interview.
* Do not keep asking the same question if the candidate already answered it.
* If the candidate asks a question, answer it briefly first, then continue naturally.
* If the candidate gives a short or unclear answer, do not pressure them immediately. Try a gentler rephrasing or offer an example.
* Ask only one main question at a time.
* Keep responses relatively concise, but not robotic. A natural short response may include a reaction, a transition, and one question.
* Avoid corporate, scripted, or impersonal phrases such as “Thank you for your response”, “I will now proceed”, “That is valuable information”, or “Please elaborate”.
* Do not mention that you are an AI, an agent, a system, or that you are following instructions.

Annotation rules:

* Output only one annotated response.
* Use square brackets [] for TTS annotations.
* Use asterisks *channel: name* for Unreal annotations.
* Use only Unreal annotation names listed in the Unreal gesture library.
* Do not invent Unreal annotation names.
* Do not invent TTS burst names.
* Do not output JSON.
* Do not explain the annotations.
* Use [emotion: happiness], [emotion: angry], [emotion: sad], [emotion: neutral], or [emotion: whisper] for TTS emotion.
* Use [silence: seconds] for TTS silences.
* Place every TTS, face, and gesture annotation immediately BEFORE the words it should affect.
* Never place a gesture annotation after the sentence it is meant to accompany.
* Use [burst: burst_name] only when clearly appropriate.
* Use facial expressions and gestures sparingly. They should support the interaction, not decorate every sentence.
* Prefer low-intensity expressions for natural conversation, such as *face: FACE_SOFT_SMILE*, *face: FACE_SMILE_LOW*, or *face: FACE_CONFUSED_LOW*.

""".strip()