from __future__ import annotations

from dialogue_manager.core.pipeline import DialoguePipeline
from dialogue_manager.core.state import DialogueState
from dialogue_manager.core.turn import UserTurnInput
from dialogue_manager.engagement.base import EngagementAnalyzer
from dialogue_manager.engagement.types import EngagementState
from dialogue_manager.llm.base import LLMClient, LLMRequest, LLMResponse
from dialogue_manager.output.schemas import DialogueManagerOutput, TTSAnnotation, UnrealAction


class MockEngagementAnalyzer(EngagementAnalyzer):
    def analyze(
        self,
        user_input: UserTurnInput,
        state: DialogueState,
    ) -> EngagementState:
        return EngagementState(
            level="medium",
            score=0.6,
            summary="Mock engagement: user seems moderately engaged.",
        )


class MockLLMClient(LLMClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        user_text = request.user_input.user_text
        engagement_level = request.engagement.level

        output = DialogueManagerOutput(
            response_text=f"I heard you say: {user_text}",
            tts_annotations=[
                TTSAnnotation(
                    text=f"I heard you say: {user_text}",
                    emotion="neutral",
                    speaking_rate=1.0,
                    pause_after_ms=300,
                )
            ],
            unreal_actions=[
                UnrealAction(
                    action_type="gesture",
                    name="nod",
                    parameters={"intensity": 0.5},
                    priority="normal",
                )
            ],
            debug={
                "engagement_level": engagement_level,
                "turn_count_before_response": request.state.turn_count,
            },
        )

        return LLMResponse(
            raw_text=output.model_dump_json(),
            parsed_output=output,
        )


def main() -> None:
    pipeline = DialoguePipeline(
        engagement_analyzer=MockEngagementAnalyzer(),
        llm_client=MockLLMClient(),
    )

    print("Mock dialogue pipeline ready.")
    print("Type something. Press Ctrl+C to stop.\n")

    while True:
        user_text = input("You: ").strip()

        if not user_text:
            continue

        output = pipeline.process_text_turn(user_text)

        print(f"Agent: {output.response_text}")
        print(f"TTS annotations: {output.tts_annotations}")
        print(f"Unreal actions: {output.unreal_actions}")
        print(f"Turn count: {pipeline.state.turn_count}")
        print()


if __name__ == "__main__":
    main()