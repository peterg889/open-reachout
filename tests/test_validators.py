import pytest

from open_reachout.core.compliance.validators import (
    Draft,
    ValidatorContext,
    Violation,
    content_hash,
    validate,
)

CTX = ValidatorContext(
    physical_address="600 Congress Ave Ste 1400, Austin TX 78701",
    unsubscribe_text="reply STOP or use this link to never hear from us again",
    sender_identity="Maya Reyes, StageMatch",
    allowed_url_prefixes=("https://stagematch.test/",),
)


def _ok_body(extra: str = "") -> str:
    return (
        "Hi Sam - saw the Thursday open-mic series on your calendar. "
        "StageMatch gives rooms like yours a free roster of vetted local acts. "
        f"{extra} Worth a look: https://stagematch.test/join \n"
        "- Maya Reyes, StageMatch\n"
        "600 Congress Ave Ste 1400, Austin TX 78701\n"
        "reply STOP or use this link to never hear from us again"
    )


def test_clean_draft_passes() -> None:
    assert validate(Draft(subject="Live music at Cactus Cafe", body=_ok_body()), CTX) == []


@pytest.mark.parametrize(
    ("subject", "code"),
    [
        ("Re: our chat", Violation.FAKE_THREAD_SUBJECT),
        ("FWD: invoice", Violation.FAKE_THREAD_SUBJECT),
        ("URGENT: act now", Violation.DECEPTIVE_SUBJECT),
    ],
)
def test_subject_lints(subject: str, code: Violation) -> None:
    codes = {f.code for f in validate(Draft(subject=subject, body=_ok_body()), CTX)}
    assert code in codes


@pytest.mark.gates
def test_gate09_can_spam_completeness() -> None:
    """Gate 9: address + unsubscribe must be present; their absence is caught."""
    body_no_addr = _ok_body().replace("600 Congress Ave Ste 1400, Austin TX 78701", "")
    codes = {f.code for f in validate(Draft(subject="hello", body=body_no_addr), CTX)}
    assert Violation.MISSING_PHYSICAL_ADDRESS in codes

    body_no_unsub = _ok_body().replace(
        "reply STOP or use this link to never hear from us again", ""
    )
    codes = {f.code for f in validate(Draft(subject="hello", body=body_no_unsub), CTX)}
    assert Violation.MISSING_UNSUBSCRIBE in codes


@pytest.mark.gates
def test_gate10_forbidden_claims_pack() -> None:
    """Gate 10: the default pack catches ROI promises and implied relationships."""
    seeded = [
        "We guarantee 10 new clients in your first month.",
        "You'll get 14 bookings within weeks.",
        "As we discussed, your profile is ready.",
        "Per our conversation last week, here's the link.",
    ]
    for sentence in seeded:
        codes = {
            f.code for f in validate(Draft(subject="hello", body=_ok_body(sentence)), CTX)
        }
        assert Violation.FORBIDDEN_CLAIM in codes, sentence


def test_url_allowlist_blocks_smuggled_links() -> None:
    body = _ok_body("Details here: https://evil.example/track?x=1")
    codes = {f.code for f in validate(Draft(subject="hello", body=body), CTX)}
    assert Violation.URL_NOT_ALLOWLISTED in codes


def test_bump_theater_on_followups_only() -> None:
    body = _ok_body("Just bumping this to the top of your inbox!")
    assert Violation.BUMP_THEATER not in {
        f.code for f in validate(Draft(subject="s", body=body, step_index=0), CTX)
    }
    assert Violation.BUMP_THEATER in {
        f.code for f in validate(Draft(subject="s", body=body, step_index=1), CTX)
    }


def test_content_hash_binds_content() -> None:
    a = Draft(subject="s", body="b")
    assert content_hash(a) == content_hash(Draft(subject="s", body="b"))
    assert content_hash(a) != content_hash(Draft(subject="s", body="b "))


# ----------------------------------------------- claim allowlist mode (FR-3.2)
from dataclasses import replace  # noqa: E402

ALLOW_CTX = replace(
    CTX,
    claim_mode="allowlist",
    approved_claims=("free roster of vetted local acts",),
    claim_registry_version="allowlist@abc123",
)


def test_allowlist_passes_approved_claim_sentences() -> None:
    assert validate(Draft(subject="Live music", body=_ok_body()), ALLOW_CTX) == []


def test_allowlist_rejects_invented_marketing_claims() -> None:
    body = _ok_body(extra="Membership is just $5/mo and we guarantee weekly bookings.")
    codes = {f.code for f in validate(Draft(subject="Live music", body=body), ALLOW_CTX)}
    assert Violation.UNREGISTERED_CLAIM in codes
    # the same invented-pricing sentence sails through denylist mode only if it
    # avoids the deny pack — allowlist is the stricter posture
    assert Violation.FORBIDDEN_CLAIM in codes or Violation.UNREGISTERED_CLAIM in codes


def test_allowlist_exempts_compliance_footer() -> None:
    # "free" appearing after the unsubscribe line (e.g. "feel free to ignore")
    # is footer text, not a product claim
    body = _ok_body() + "\nPS this footer mentions free stuff after unsubscribe"
    assert validate(Draft(subject="Live music", body=body), ALLOW_CTX) == []


def test_registry_version_tracks_claim_set() -> None:
    from open_reachout.core.compliance.claims import registry_version
    from open_reachout.core.config import AboutUs, IdentitySpec

    identity = IdentitySpec(
        sender="Maya Reyes, StageMatch",
        physical_address="600 Congress Ave Ste 1400, Austin TX 78701",
    )
    base = dict(name="StageMatch", what_we_do="free venue accounts for live music",
                identity=identity)
    deny = AboutUs(**base)
    assert registry_version(deny) == "deny-pack@1"
    a1 = AboutUs(**base, claims_mode="allowlist", approved_claims=["free venue accounts"])
    a2 = AboutUs(**base, claims_mode="allowlist",
                 approved_claims=["free venue accounts", "band membership $9/mo"])
    assert registry_version(a1).startswith("allowlist@")
    assert registry_version(a1) != registry_version(a2)  # version moves with the set


def test_allowlist_mode_requires_claims() -> None:
    from pydantic import ValidationError

    from open_reachout.core.config import AboutUs, IdentitySpec

    identity = IdentitySpec(
        sender="Maya Reyes, StageMatch",
        physical_address="600 Congress Ave Ste 1400, Austin TX 78701",
    )
    with pytest.raises(ValidationError, match="requires at least one"):
        AboutUs(name="X", what_we_do="free venue accounts", identity=identity,
                claims_mode="allowlist")


# ------------------------------------------------ collateral assets (FR-3.10)
def _about(assets: list[dict], **kw):  # noqa: ANN202
    from open_reachout.core.config import AboutUs, IdentitySpec

    identity = IdentitySpec(
        sender="Maya Reyes, StageMatch",
        physical_address="600 Congress Ave Ste 1400, Austin TX 78701",
    )
    return AboutUs(name="StageMatch", what_we_do="free venue accounts",
                   identity=identity, assets=assets, **kw)


def test_asset_registration_lints_claims() -> None:
    from pydantic import ValidationError

    ok = _about([{"id": "venue_onepager", "url": "https://stagematch.test/one.pdf",
                  "summary": "How StageMatch booking works for venues"}])
    assert ok.assets[0].id == "venue_onepager"
    with pytest.raises(ValidationError, match="forbidden claim"):
        _about([{"id": "bad", "url": "https://stagematch.test/b.pdf",
                 "summary": "Guaranteed bookings every weekend for your venue"}])
    with pytest.raises(ValidationError, match="unregistered claim"):
        _about([{"id": "sneaky", "url": "https://stagematch.test/s.pdf",
                 "summary": "Our platform is just $5/mo for unlimited gigs"}],
               claims_mode="allowlist", approved_claims=["free venue accounts"])
    with pytest.raises(ValidationError, match="https"):
        _about([{"id": "http_bad", "url": "http://x.test/a.pdf", "summary": "plain doc"}])


def test_asset_slot_resolves_to_vetted_url_and_passes_validators() -> None:
    from pathlib import Path

    from open_reachout.core import dryrun
    from open_reachout.core.config import load_tenant

    raw = load_tenant(
        Path(__file__).resolve().parents[1] / "examples" / "music-marketplace" / "tenant.yaml"
    ).model_dump()
    raw["brief"]["about_us"]["assets"] = [
        {"id": "venue_onepager", "url": "https://stagematch.test/venues.pdf",
         "summary": "How StageMatch booking works for venues"}
    ]
    from open_reachout.core.config import TenantConfig

    cfg = TenantConfig.model_validate(raw)
    ctx = dryrun.validator_context(cfg)
    assert "https://stagematch.test/venues.pdf" in ctx.allowed_url_prefixes
    body = (
        "Saw the Thursday series on your calendar. "
        "Our one-pager: https://stagematch.test/venues.pdf\n"
        f"- {ctx.sender_identity}\n{ctx.physical_address}\n{ctx.unsubscribe_text}"
    )
    assert validate(Draft(subject="Live music", body=body), ctx) == []


# --------------------------------------------- PHI / sector screen (FR-3.11)
PHI_CTX = replace(CTX, sector_sensitivity="healthcare")


@pytest.mark.parametrize(
    "leak",
    [
        "Her DOB: 04/12/1989 and she prefers mornings.",
        "MRN 84JK-22 attached for context.",
        "He was diagnosed with F41.1 last spring.",
        "Sharing the treatment plan for a mutual contact.",
        "My client mentioned your practice during session.",
    ],
)
def test_phi_screen_blocks_care_information(leak: str) -> None:
    codes = {f.code for f in validate(Draft(subject="hello", body=_ok_body(leak)), PHI_CTX)}
    assert Violation.PHI_SUSPECTED in codes


def test_phi_screen_passes_provider_to_provider_copy() -> None:
    copy = (
        "Therapists tell us referrals from directories have collapsed. "
        "TheraDirectory verifies every profile and is free until your first inquiry."
    )
    findings = validate(Draft(subject="A verified profile", body=_ok_body(copy)), PHI_CTX)
    assert Violation.PHI_SUSPECTED not in {f.code for f in findings}


def test_phi_screen_off_for_default_sector() -> None:
    body = _ok_body("Her DOB: 04/12/1989.")
    assert Violation.PHI_SUSPECTED not in {
        f.code for f in validate(Draft(subject="s", body=body), CTX)
    }


# ----------------------------------------------- compliance regimes (FR-7.7)
def test_regime_extras_compose_additively() -> None:
    from open_reachout.core.compliance.regimes import (
        ComplianceRegime,
        get_regime,
        register_regime,
    )
    from open_reachout.core.compliance.validators import Finding

    def no_exclamations(draft: Draft, ctx: ValidatorContext) -> list[Finding]:
        if "!" in draft.body:
            return [Finding(Violation.FORBIDDEN_CLAIM, "strict regime: no exclamations")]
        return []

    try:
        register_regime(ComplianceRegime(name="strictland",
                                         extra_validators=(no_exclamations,)))
    except ValueError:
        pass  # already registered by a prior test in this session
    strict = replace(CTX, compliance_regime="strictland")
    findings = validate(Draft(subject="hello", body=_ok_body("So exciting!")), strict)
    assert any("no exclamations" in f.detail for f in findings)
    # the core pack still ran (additive composition): strip the address and
    # BOTH the regime extra and the core CAN-SPAM finding appear
    body = _ok_body("So exciting!").replace(
        "600 Congress Ave Ste 1400, Austin TX 78701", "")
    codes = {f.code for f in validate(Draft(subject="hello", body=body), strict)}
    assert Violation.MISSING_PHYSICAL_ADDRESS in codes
    # regimes are immutable: re-registration (i.e. weakening) is refused
    import pytest as _pytest

    with _pytest.raises(ValueError, match="immutable"):
        register_regime(ComplianceRegime(name="us_can_spam"))
    assert get_regime("us_can_spam").name == "us_can_spam"


def test_unknown_regime_rejected_at_config() -> None:
    from pydantic import ValidationError

    from open_reachout.core.config import AboutUs, IdentitySpec

    identity = IdentitySpec(
        sender="Maya Reyes, StageMatch",
        physical_address="600 Congress Ave Ste 1400, Austin TX 78701",
    )
    with pytest.raises(ValidationError, match="unknown compliance regime"):
        AboutUs(name="X", what_we_do="free venue accounts", identity=identity,
                compliance_regime="narnia_pecr")
