import pytest
from hypothesis import given
from hypothesis import strategies as st

from open_reachout.core.canonical import InvalidEmailError, canonicalize, tombstone_hash


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Alice@Example.COM", "alice@example.com"),
        ("  bob@example.com  ", "bob@example.com"),
        ("carol+tag@example.com", "carol@example.com"),
        ("carol+a+b@example.com", "carol@example.com"),
        ("d.a.v.e@gmail.com", "dave@gmail.com"),
        ("d.a.v.e+promo@GoogleMail.com", "dave@gmail.com"),
        ("dots.kept@fastmail.com", "dots.kept@fastmail.com"),
    ],
)
def test_canonical_rules(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


# Gate 8 alias matrix (PRD section 10): all variants of one mailbox collapse.
GATE8_ALIASES = [
    "jane.doe@gmail.com",
    "janedoe@gmail.com",
    "Jane.Doe+news@googlemail.com",
    "JANEDOE+x@GMAIL.COM",
]


@pytest.mark.gates
def test_gate08_alias_suppression_matrix() -> None:
    canon = {canonicalize(a) for a in GATE8_ALIASES}
    assert canon == {"janedoe@gmail.com"}
    assert len({tombstone_hash(a) for a in GATE8_ALIASES}) == 1


@pytest.mark.parametrize("bad", ["", "nope", "a@b", "@x.com", "a b@x.com", "+only@x.com"])
def test_invalid_rejected(bad: str) -> None:
    with pytest.raises(InvalidEmailError):
        canonicalize(bad)


@given(
    st.from_regex(
        r"[a-z][a-z0-9.]{0,10}(\+[a-z0-9]{1,5})?@[a-z]{1,10}\.(com|org|io)", fullmatch=True
    )
)
def test_idempotent(email: str) -> None:
    once = canonicalize(email)
    assert canonicalize(once) == once
