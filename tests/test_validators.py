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
