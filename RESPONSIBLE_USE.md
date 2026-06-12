# Responsible Use

Open Reachout automates cold outreach. Cold outreach done badly is spam, and
spam harms real people and burns the commons (deliverability, community trust,
the recipient's afternoon). This project's answer is structural, not
aspirational: the safety and compliance layer lives in the core and is
**non-bypassable by config, plugins, or agents** (PRD §10, engineering spec §2).

## What the framework enforces in code

- **CAN-SPAM completeness** in every message: physical address, working
  unsubscribe, truthful subjects, sender identity honesty. Validators run
  twice and bind to a content hash.
- **Suppression-first sending** with alias-aware canonicalization; opt-outs
  propagate in minutes, not the legal 10 business days.
- **Volume, frequency, and spend caps**: per-inbox daily limits, entity-level
  cross-campaign frequency caps, an annual touch ceiling, hard budget gates.
- **≤3 follow-ups**, minimum 3-day gaps — constants, not configuration.
- **No fake-human personas, no "just bumping this" follow-ups, no smuggled
  URLs** — rejected by validation.
- **Halt and kill switches** that only a human can resume; **one-call
  data-subject deletion** with provider propagation.
- **No scraping of denylisted or login-gated sources**; every prospect record
  carries provenance and a declared data basis.

## What we deliberately do not ship

- SMTP sending (use a cold-email provider whose abuse policies stay in the loop)
- Contact databases or purchased lists (imports without provenance are rejected)
- Spintax, open-tracking cloaks, suppression workarounds, deliverability
  evasion of any kind — PRs adding these will be declined
- Telemetry of any sort

## Your obligations as an operator

You remain legally responsible for your outreach. Before sending:
read the FTC's CAN-SPAM guidance (and your jurisdiction's equivalent),
configure a real physical address and reachable sender identity, keep your
spam-complaint rate under 0.1%, and honor every opt-out everywhere, forever.
If you are using this to reach audiences outside the US, stop and read the
relevant regime first (CASL and GDPR/PECR are not opt-out regimes).

If you can't explain to a recipient — or a journalist — how you found them
and why you contacted them, don't send the email. The framework will show
you exactly what that answer would be (`per-prospect audit export`).
