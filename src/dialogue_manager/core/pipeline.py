from __future__ import annotations

from dialogue_manager.core.state import DialogueState
from dialogue_manager.core.turn import DialogueTurn, UserTurnInput
from dialogue_manager.engagement.base import EngagementAnalyzer
from dialogue_manager.engagement.types import EngagementState
from dialogue_manager.llm.base import LLMClient, LLMRequest
from dialogue_manager.output.schemas import DialogueManagerOutput


class DialoguePipeline:
    """
    Central dialogue manager pipeline.

    This class coordinates the main modules but does not know their internal details.
    It does not know whether STT is Whisper, whether the LLM is Qwen, or whether
    Unreal is connected via sockets, HTTP, or something else.
    """

    def __init__(
        self,
        engagement_analyzer: EngagementAnalyzer,
        llm_client: LLMClient,
        state: DialogueState | None = None,
    ) -> None:
        self.engagement_analyzer = engagement_analyzer
        self.llm_client = llm_client
        self.state = state or DialogueState()

        def _build_score_only_engagement_state(self,engagement_score: float | None,) -> EngagementState:
            """
            Build the minimal engagement object sent to the LLM request.

            Important: this object contains only the latest numeric score and basic
            availability metadata. It does not contain any policy such as "ask a
            shorter question" or "repair the interaction". That policy lives in the
            system prompt.
            """
            ready = engagement_score is not None

            if engagement_score is None:
                score = 0.5
            else:
                score = max(0.0, min(1.0, float(engagement_score)))

            return EngagementState(
                score=score,
                summary=f"Realtime engagement score: {score:.3f}.",
                metadata={
                    "source": "get_latest_score",
                    "ready": ready,
                },
            )

    def process_text_turn(
        self,
        user_text: str,
    ) -> DialogueManagerOutput:
        """
        Process one user turn from already-transcribed text.
        The realtime engagement score is read once, immediately before building
        the LLM request. If the caller already read the score, it can pass it in
        explicitly so that logging and prompt construction use the same value.
        """
        engagement_score: float | None = None,

        user_input = UserTurnInput(user_text=user_text)

        if engagement_score is None:
            engagement_score = self.engagement_analyzer.get_latest_score()

        engagement = self._build_score_only_engagement_state(engagement_score)

        llm_request = LLMRequest(
            user_input=user_input,
            engagement=engagement,
            state=self.state,
            metadata={
                "engagement_score_source": "get_latest_score",
                "engagement_score_ready": engagement.metadata.get("ready", False),
                "engagement_score_used": engagement.score,
            },
        )

        llm_response = self.llm_client.generate(llm_request)

        if llm_response.parsed_output is None:
            raise ValueError(
                "LLM response did not contain a valid parsed DialogueManagerOutput."
            )

        turn = DialogueTurn(
            user_input=user_input,
            engagement=engagement,
            output=llm_response.parsed_output,
            raw_llm_output=llm_response.raw_text,
        )

        self.state.add_turn(turn)

        return llm_response.parsed_output

    def reset(self) -> None:
        """
        Reset the dialogue state.
        """

        self.state = DialogueState()