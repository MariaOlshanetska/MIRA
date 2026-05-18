from __future__ import annotations

from dialogue_manager.core.state import DialogueState
from dialogue_manager.core.turn import DialogueTurn, UserTurnInput
from dialogue_manager.engagement.base import EngagementAnalyzer
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

    def process_text_turn(
        self,
        user_text: str,
    ) -> DialogueManagerOutput:
        """
        Process one user turn from already-transcribed text.
        """

        user_input = UserTurnInput(user_text=user_text)

        engagement = self.engagement_analyzer.analyze(
            user_input=user_input,
            state=self.state,
        )

        llm_request = LLMRequest(
            user_input=user_input,
            engagement=engagement,
            state=self.state,
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