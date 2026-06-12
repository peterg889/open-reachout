"""Injection corpus runner (spec 9.4): regressions here block release (gate 2)."""

from pathlib import Path

import pytest
import yaml

from open_reachout.core.variables import ResolvedValue, TrustClass, marker_for, resolve
from open_reachout.security.envelope import injection_suspects, wrap

CORPUS = yaml.safe_load((Path(__file__).parent / "injection" / "vectors.yaml").read_text())
EVIDENCE_VECTORS = [v for v in CORPUS["vectors"] if v["channel"] == "evidence"]
PROMPT = "Mention {{evidence.note}} to {{prospect.first_name}}."


def _resolved(payload: str):
    return resolve(
        PROMPT,
        {
            "evidence.note": ResolvedValue(
                "evidence.note", payload, TrustClass.UNTRUSTED, fact_id="f-x"
            ),
            "prospect.first_name": ResolvedValue(
                "prospect.first_name", "Sam", TrustClass.PROSPECT
            ),
        },
    )


@pytest.mark.gates
@pytest.mark.disqualifying
@pytest.mark.parametrize("vector", EVIDENCE_VECTORS, ids=lambda v: v["id"])
def test_evidence_payloads_never_inline(vector: dict) -> None:
    """Structural defense: untrusted values resolve to markers, not text."""
    resolved = _resolved(vector["payload"])
    assert vector["payload"] not in resolved.text
    assert marker_for("evidence.note") in resolved.text


@pytest.mark.gates
@pytest.mark.disqualifying
@pytest.mark.parametrize(
    "vector",
    [v for v in EVIDENCE_VECTORS if v["expect"] == "heuristic_escalation"],
    ids=lambda v: v["id"],
)
def test_heuristic_tripwires_fire(vector: dict) -> None:
    assert injection_suspects(vector["payload"]) != []


@pytest.mark.gates
@pytest.mark.disqualifying
def test_envelope_escape_is_neutralized() -> None:
    vector = next(v for v in CORPUS["vectors"] if v["id"] == "envelope_escape")
    env = wrap(vector["payload"], source="web", idem="f-x")
    # The close tag inside the payload is escaped: exactly one real close tag,
    # at the very end of the envelope.
    assert env.text.count("</untrusted>") == 1
    assert env.text.strip().endswith("</untrusted>")


# link_smuggle is covered end-to-end in test_composer_qualifier.py
# (test_gate02_smuggled_url_is_rejected): the URL allowlist validator rejects
# any outbound URL not present in tenant config, after compose retries.
