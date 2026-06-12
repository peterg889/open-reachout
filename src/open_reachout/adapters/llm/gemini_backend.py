"""Gemini LLM backend (default live backend) — structured outputs via
response_schema.

Optional dependency: `pip install google-genai`. BYO key via GEMINI_API_KEY
(or GOOGLE_API_KEY).

Model tiers follow spec D-6: high-frequency pipeline tasks (compose, qualify,
classify, groundedness, extraction) run on the fast tier; low-frequency,
high-reasoning tasks (program synthesis, discovery research, goal
brainstorming) run on the reasoning tier. Both are operator-configurable —
set newer model IDs in config as Google ships them.
"""

from __future__ import annotations

from pydantic import BaseModel

REASONING_MODEL_DEFAULT = "gemini-2.5-pro"
FAST_MODEL_DEFAULT = "gemini-2.5-flash"

_REASONING_TASKS = {"synthesize_program", "discovery_research", "brainstorm_goals", "winloss"}

_MAX_TOKENS = {"compose": 2048, "groundedness": 1024, "qualify": 1024, "classify_reply": 512}
_MAX_TOKENS_DEFAULT = 4096


class GeminiBackend:
    """Implements core.interfaces.LLMBackend."""

    def __init__(
        self,
        reasoning_model: str = REASONING_MODEL_DEFAULT,
        fast_model: str = FAST_MODEL_DEFAULT,
    ) -> None:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "the google-genai package is required for GeminiBackend: "
                "pip install google-genai"
            ) from exc
        self._client = genai.Client()  # credentials resolved from environment
        self.reasoning_model = reasoning_model
        self.fast_model = fast_model

    def model_for(self, task: str) -> str:
        return self.reasoning_model if task in _REASONING_TASKS else self.fast_model

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        response = self._client.models.generate_content(
            model=self.model_for(task),
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": schema,
                "max_output_tokens": _MAX_TOKENS.get(task, _MAX_TOKENS_DEFAULT),
            },
        )
        parsed = response.parsed
        if parsed is None:  # blocked or unparseable — fail closed, job retries/escalates
            raise RuntimeError(f"LLM task {task!r} returned no parseable output")
        if not isinstance(parsed, schema):
            return schema.model_validate(parsed)
        return parsed
