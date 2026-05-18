from __future__ import annotations

import os
import re
from typing import Any

import requests
from dotenv import load_dotenv

from dialogue_manager.llm.base import LLMClient, LLMRequest, LLMResponse
from dialogue_manager.llm.prompt_builder import build_system_prompt, build_user_prompt
from dialogue_manager.output.annotation_parser import AnnotationParseError, parse_annotated_response


def _clean_llm_text(text: str) -> str:
    """
    Remove common wrappers that LLMs sometimes add despite instructions.
    """

    text = text.strip()

    # Remove possible Qwen thinking blocks.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Remove Markdown code fences if any.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # Remove accidental surrounding quotes.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()

    return text.strip()


class QwenLLMClient(LLMClient):
    """
    OpenAI-compatible client for the lab Qwen server.

    Expected endpoint:
        {base_url}/chat/completions

    Example:
        http://10.10.200.182:8004/v1/chat/completions
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: int | None = None,
        api_key: str | None = None,
    ) -> None:
        load_dotenv()

        self.base_url = (
            base_url
            or os.getenv("QWEN_API_BASE_URL")
            or "http://10.10.200.182:8004/v1"
        ).rstrip("/")

        self.model = (
            model
            or os.getenv("QWEN_MODEL")
            or "QuantTrio/Qwen3-VL-30B-A3B-Instruct-AWQ"
        )

        self.max_tokens = int(max_tokens or os.getenv("QWEN_MAX_TOKENS") or 512)
        self.temperature = float(temperature or os.getenv("QWEN_TEMPERATURE") or 0.7)
        self.timeout_seconds = int(timeout_seconds or os.getenv("QWEN_TIMEOUT_SECONDS") or 60)
        self.api_key = api_key or os.getenv("QWEN_API_KEY")

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def generate(self, request: LLMRequest) -> LLMResponse:
        system_prompt = request.system_prompt or build_system_prompt()
        user_prompt = build_user_prompt(request)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        headers = {
            "Content-Type": "application/json",
        }

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        response = requests.post(
            self.chat_completions_url,
            headers=headers,
            json=payload,
            timeout=self.timeout_seconds,
        )

        response.raise_for_status()
        data = response.json()

        try:
            raw_text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Qwen API response format: {data}") from exc

        cleaned_text = _clean_llm_text(raw_text)

        try:
            parsed_output = parse_annotated_response(cleaned_text)
        except AnnotationParseError:
            parsed_output = None

        return LLMResponse(
            raw_text=cleaned_text,
            parsed_output=parsed_output,
            metadata={
                "provider": "qwen",
                "model": self.model,
                "url": self.chat_completions_url,
                "usage": data.get("usage"),
            },
        )