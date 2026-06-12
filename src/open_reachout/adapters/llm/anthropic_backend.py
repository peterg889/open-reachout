"""Anthropic LLM backend (spec D-6) — structured outputs via messages.parse().

Optional dependency: `pip install anthropic`. BYO key via ANTHROPIC_API_KEY.

Model tiers follow spec D-6: high-frequency pipeline tasks (compose, qualify,
classify, groundedness, extraction) run on the fast tier; low-frequency,
high-reasoning tasks (program synthesis, discovery research, goal
brainstorming) run on the reasoning tier. Both are operator-configurable.
"""

from __future__ import annotations

from pydantic import BaseModel

REASONING_MODEL_DEFAULT = "claude-opus-4-8"
FAST_MODEL_DEFAULT = "claude-sonnet-4-6"

_REASONING_TASKS = {
    "synthesize_program", "discovery_research", "brainstorm_goals", "winloss_synth",
}

_MAX_TOKENS = {"compose": 2048, "groundedness": 1024, "qualify": 1024, "classify_reply": 512}
_MAX_TOKENS_DEFAULT = 4096


class AnthropicBackend:
    """Implements core.interfaces.LLMBackend."""

    def __init__(
        self,
        reasoning_model: str = REASONING_MODEL_DEFAULT,
        fast_model: str = FAST_MODEL_DEFAULT,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "the anthropic package is required for AnthropicBackend: pip install anthropic"
            ) from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()  # credentials resolved from environment
        self.reasoning_model = reasoning_model
        self.fast_model = fast_model

    def model_for(self, task: str) -> str:
        return self.reasoning_model if task in _REASONING_TASKS else self.fast_model

    def complete(self, task: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
        response = self._client.messages.parse(
            model=self.model_for(task),
            max_tokens=_MAX_TOKENS.get(task, _MAX_TOKENS_DEFAULT),
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        parsed = response.parsed_output
        if parsed is None:  # refusal or unparseable — fail closed, job retries/escalates
            raise RuntimeError(
                f"LLM task {task!r} returned no parseable output "
                f"(stop_reason={response.stop_reason!r})"
            )
        return parsed
