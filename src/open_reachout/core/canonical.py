"""Email canonicalization (engineering spec section 13.1, invariant I-3).

All suppression, tombstone, and uniqueness logic operates on the canonical
form. The rules are deliberately conservative: when in doubt, two addresses
collapse to the same canonical form so that suppression covers more, never
less.

Rules:
- trim whitespace, lowercase
- strip a single ``+suffix`` from the local part (all domains)
- gmail.com / googlemail.com: remove dots in the local part, normalize the
  domain to gmail.com
- IDN domains are encoded to punycode
"""

from __future__ import annotations

import hashlib
import re

_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}

# Pragmatic shape check: one @, non-empty local and domain with a dot.
# Full RFC 5321 validation is the verifier adapter's job, not ours.
_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidEmailError(ValueError):
    """Raised when an address cannot be canonicalized."""


def canonicalize(email: str) -> str:
    """Return the canonical form of *email*.

    Idempotent: ``canonicalize(canonicalize(x)) == canonicalize(x)``.
    Raises :class:`InvalidEmailError` on malformed input (fail closed: an
    address we cannot canonicalize is an address we refuse to contact).
    """
    candidate = email.strip().lower()
    if not _SHAPE.match(candidate):
        raise InvalidEmailError(f"not a plausible email address: {email!r}")

    local, _, domain = candidate.rpartition("@")

    # Strip everything from the first '+' (conservative: suppress more).
    plus = local.find("+")
    if plus >= 0:
        local = local[:plus]
    if not local:
        raise InvalidEmailError(f"empty local part after canonicalization: {email!r}")

    # IDN -> punycode.
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:  # pragma: no cover - exotic inputs
        raise InvalidEmailError(f"undecodable domain in {email!r}") from exc

    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
        if not local:
            raise InvalidEmailError(f"empty local part after canonicalization: {email!r}")

    return f"{local}@{domain}"


def tombstone_hash(email: str) -> str:
    """SHA-256 of the canonical form — the only thing `forget` leaves behind (I-6)."""
    return hashlib.sha256(canonicalize(email).encode("utf-8")).hexdigest()
