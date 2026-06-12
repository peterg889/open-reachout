# Security Policy

Open Reachout processes adversarial input by design: it scrapes the open web
and reads strangers' email replies. The threat model and structural defenses
are documented in [`docs/engineering-spec.md`](docs/engineering-spec.md)
(sections 2, 8.7, 9.3–9.4).

## Reporting a vulnerability

Email the maintainer (see repository profile) or use GitHub private
vulnerability reporting. Please do not open public issues for: prompt-injection
bypasses of the untrusted-content envelope, gatekeeper/claim-path bypasses,
suppression or halt bypasses, or webhook authentication flaws. These are the
crown jewels; we treat reports against them as release-blocking.

Supported versions: the latest minor release.
