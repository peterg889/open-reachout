"""The composer: prompt + variables -> validated, grounded draft (spec 8.4).

Pure agent module (spec 20): LLM tasks in, validated structures out. No DB,
no providers, no side effects. The caller persists results and runs the
gatekeeper claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from open_reachout.agents.prompts import COMPOSE_FRAME, GROUNDEDNESS_FRAME, PROMPT_VERSIONS
from open_reachout.agents.schemas import ComposeOutput, GroundednessOutput
from open_reachout.core.compliance.validators import (
    Draft,
    ValidatorContext,
    content_hash,
    validate,
)
from open_reachout.core.interfaces import LLMBackend
from open_reachout.core.variables import ResolvedPrompt, ResolvedValue, resolve
from open_reachout.security.envelope import injection_suspects, wrap

MAX_COMPOSE_RETRIES = 2


class ComposeEscalation(Exception):
    """Composition could not produce a compliant draft; a human must look."""


@dataclass(frozen=True)
class ComposeInputs:
    variant_id: str
    variant_prompt: str
    values: dict[str, ResolvedValue]  # resolved variable values incl. evidence
    validator_ctx: ValidatorContext
    trusted_context: str  # sender identity/address/unsubscribe/links lines
    step_index: int = 0


@dataclass(frozen=True)
class ComposeResult:
    draft: Draft
    output: ComposeOutput
    content_sha256: str
    groundedness_passed_hash: str | None
    resolved: ResolvedPrompt
    prompt_versions: dict[str, str] = field(default_factory=dict)


def _evidence_blocks(resolved: ResolvedPrompt) -> tuple[str, set[str]]:
    blocks: list[str] = []
    fact_ids: set[str] = set()
    for value in resolved.untrusted:
        idem = value.fact_id or value.slot
        fact_ids.add(idem)
        blocks.append(f"fact_id={idem}\n{wrap(value.value, source='web', idem=idem).text}")
    return "\n\n".join(blocks) or "(no evidence provided)", fact_ids


def compose(llm: LLMBackend, inputs: ComposeInputs) -> ComposeResult:
    """Generate, validate, and groundedness-audit one draft.

    Raises :class:`ComposeEscalation` after MAX_COMPOSE_RETRIES failed
    attempts or on any injection signal — never returns a non-compliant draft.
    """
    resolved = resolve(inputs.variant_prompt, inputs.values)
    if any(injection_suspects(v.value) for v in resolved.untrusted):
        raise ComposeEscalation("injection heuristics flagged evidence content")

    evidence_blocks, known_fact_ids = _evidence_blocks(resolved)
    feedback = ""
    for _attempt in range(1 + MAX_COMPOSE_RETRIES):
        prompt = COMPOSE_FRAME.format(
            variant_prompt=resolved.text,
            trusted_context=inputs.trusted_context + feedback,
            evidence_blocks=evidence_blocks,
        )
        output = llm.complete("compose", prompt, ComposeOutput)
        assert isinstance(output, ComposeOutput)
        if output.injection_suspected:
            raise ComposeEscalation("composer reported suspected injection")

        problems: list[str] = []
        unknown = [c.fact_id for c in output.claims if c.fact_id not in known_fact_ids]
        if unknown:
            problems.append(f"claims cite unknown fact_ids: {unknown}")

        draft = Draft(subject=output.subject, body=output.body, step_index=inputs.step_index)
        problems += [f"{f.code}: {f.detail}" for f in validate(draft, inputs.validator_ctx)]

        if not problems:
            digest = content_hash(draft)
            grounded = _audit(llm, draft, output, evidence_blocks)
            if not grounded:
                raise ComposeEscalation("groundedness audit failed (invariant I-4)")
            return ComposeResult(
                draft=draft,
                output=output,
                content_sha256=digest,
                groundedness_passed_hash=digest,
                resolved=resolved,
                prompt_versions={
                    "compose": PROMPT_VERSIONS["compose"],
                    "groundedness": PROMPT_VERSIONS["groundedness"],
                },
            )
        feedback = (
            "\n\nYour previous draft was rejected by validators. Fix ALL of the "
            "following and regenerate:\n- " + "\n- ".join(problems)
        )
    raise ComposeEscalation(f"no compliant draft after {MAX_COMPOSE_RETRIES + 1} attempts")


def _audit(
    llm: LLMBackend, draft: Draft, output: ComposeOutput, evidence_blocks: str
) -> bool:
    """Independent LLM groundedness check (gate 1). Fail closed on disagreement."""
    prompt = GROUNDEDNESS_FRAME.format(
        subject=draft.subject,
        body=draft.body,
        claims="\n".join(f"- [{c.fact_id}] {c.text}" for c in output.claims) or "(none)",
        evidence_blocks=evidence_blocks,
    )
    audit = llm.complete("groundedness", prompt, GroundednessOutput)
    assert isinstance(audit, GroundednessOutput)
    return audit.grounded and not audit.injection_suspected
