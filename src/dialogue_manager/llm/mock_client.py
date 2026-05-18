from __future__ import annotations

from dialogue_manager.llm.base import LLMClient, LLMRequest, LLMResponse
from dialogue_manager.output.annotation_parser import parse_annotated_response


class MockLLMClient(LLMClient):
    """
    Temporary LLM client.

    It returns an annotated response in the format we expect from the real LLM.
    Later this will be replaced by the Qwen API client.
    """

    def generate(self, request: LLMRequest) -> LLMResponse:
        user_text = request.user_input.user_text
        engagement = request.engagement

        if engagement.level in {"low", "very_low"}:
            annotated_response = (
                "[calm] [slow] I heard you *face: attentive*. "
                f"You said: {user_text}. "
                "Could you tell me a little more? *gesture: small_nod*"
            )

        elif engagement.level in {"high", "very_high"}:
            annotated_response = (
                "[warm] Great *face: smile*. "
                f"You said: {user_text}. "
                "Let's keep going with that energy. *gesture: open_gesture*"
            )

        else:
            annotated_response = (
                "[neutral] I heard you *gaze: look_at_user*. "
                f"You said: {user_text}. "
                "Please continue. *gesture: nod*"
            )

        output = parse_annotated_response(annotated_response)

        output.debug.update(
            {
                "mock_llm": True,
                "engagement_level": engagement.level,
                "engagement_score": engagement.score,
                "turn_count_before_response": request.state.turn_count,
            }
        )

        return LLMResponse(
            raw_text=annotated_response,
            parsed_output=output,
        )