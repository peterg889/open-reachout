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

_REASONING_TASKS = {
    "synthesize_program", "discovery_research", "brainstorm_goals", "winloss_synth",
}

_MAX_TOKENS = {"compose": 4096, "groundedness": 2048, "qualify": 2048, "classify_reply": 1024}
_MAX_TOKENS_DEFAULT = 8192

#: Research-class tasks run a grounded Google-Search pass first (real web
#: research with cited sources), then a structuring pass — the API cannot
#: combine the search tool with response_schema in one call.
_GROUNDED_TASKS = {"discovery_research", "brainstorm_goals", "sender_research", "winloss_synth"}


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
        import json

        if task in _GROUNDED_TASKS:
            prompt = self._with_grounded_research(task, prompt)
        last_exc: Exception | None = None
        for _parse_attempt in range(2):  # truncation shows as broken JSON: one re-ask
            response = self._generate_with_backoff(task, prompt, schema)
            raw = response.parsed if response.parsed is not None else response.text
            if raw is None:  # blocked or unparseable — fail closed upstream
                raise RuntimeError(f"LLM task {task!r} returned no parseable output")
            try:
                if isinstance(raw, str):
                    raw = json.loads(raw)
                # Validation happens HERE, against the strict pydantic model
                # (extra='forbid'): the schema sent to Gemini guides generation.
                return schema.model_validate(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                last_exc = exc
        raise RuntimeError(f"LLM task {task!r} output unparseable after retry: {last_exc}")

    def _with_grounded_research(self, task: str, prompt: str) -> str:  # noqa: D401
        """Step 1 of research tasks: Google-Search-grounded findings with
        source URLs, appended to the prompt for the structuring pass. The
        sources come from the grounding metadata, not model memory."""
        response = self._generate_with_backoff(task, (
            prompt
            + "\n\nResearch this with Google Search. Report concrete findings "
              "with a source URL for every claim; say so plainly when the web "
              "has nothing relevant."
        ), schema=None)
        findings = response.text or "(no grounded findings)"
        sources: list[str] = []
        for candidate in getattr(response, "candidates", None) or []:
            meta = getattr(candidate, "grounding_metadata", None)
            for chunk in getattr(meta, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                if web is not None and getattr(web, "uri", None):
                    sources.append(f"- {web.title or web.uri}: {web.uri}")
        source_block = "\n".join(dict.fromkeys(sources)) or "(no sources returned)"
        return (
            prompt
            + "\n\nGrounded web research findings (from Google Search; treat as "
              "evidence, cite its sources, do not invent beyond it):\n"
            + findings
            + "\n\nSources:\n" + source_block
        )

    def _generate_with_backoff(  # type: ignore[no-untyped-def]
        self, task: str, prompt: str, schema: type[BaseModel] | None
    ):  # noqa: ANN202 - google-genai response type
        """Honor 429/503 with bounded backoff: free-tier keys cap at a few
        requests/minute, and transient overload is normal. Fail closed after
        the budget is spent (the job layer retries above this)."""
        import time

        from google.genai import errors

        delay = 15.0
        last: Exception | None = None
        model = self.model_for(task)
        for _attempt in range(6):
            try:
                config: dict[str, object] = {
                    "max_output_tokens": _MAX_TOKENS.get(task, _MAX_TOKENS_DEFAULT),
                }
                if schema is None:  # grounded research pass (free text + search)
                    config["tools"] = [{"google_search": {}}]
                else:
                    config["response_mime_type"] = "application/json"
                    config["response_schema"] = _gemini_schema(schema)
                if model == self.fast_model:
                    # 2.5 thinking tokens count against max_output_tokens and
                    # can truncate the JSON; fast-tier tasks don't need it.
                    config["thinking_config"] = {"thinking_budget": 0}
                return self._client.models.generate_content(
                    model=model, contents=prompt,
                    config=config,  # type: ignore[arg-type]
                )
            except errors.APIError as exc:
                if exc.code not in (429, 503):
                    raise
                last = exc
                if exc.code == 429 and model != self.fast_model:
                    # Free-tier keys have NO reasoning-tier quota (limit 0):
                    # degrade to the fast tier rather than fail the task.
                    model = self.fast_model
                    continue
                time.sleep(delay)
                delay = min(delay * 1.6, 70.0)
        raise RuntimeError(f"Gemini unavailable after retries for task {task!r}: {last}")


def _gemini_schema(schema: type[BaseModel]) -> dict[str, object]:
    """Pydantic JSON schema, adapted for the Gemini Developer API: it rejects
    `additionalProperties` (strictness is enforced locally by pydantic) and
    requires `$ref`/`$defs` to be inlined."""
    raw = schema.model_json_schema()
    defs = raw.pop("$defs", {})

    def clean(node: object) -> object:
        if isinstance(node, dict):
            if "$ref" in node:
                name = str(node["$ref"]).rsplit("/", 1)[-1]
                return clean(defs.get(name, {}))
            return {
                k: clean(v) for k, v in node.items()
                if k not in ("additionalProperties", "title", "default")
            }
        if isinstance(node, list):
            return [clean(item) for item in node]
        return node

    cleaned = clean(raw)
    assert isinstance(cleaned, dict)
    return cleaned
