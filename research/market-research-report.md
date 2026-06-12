# Market Research Report: Agentic Marketplace-Supply Outreach

**Date:** June 2026
**Method:** 5 parallel research streams (50+ web searches, ~30 primary-source fetches), followed by a 3-voter adversarial verification pass over the load-bearing claims. Verification verdicts are noted inline; claims that failed or weakened under verification have been corrected or flagged.

**Question:** Does a fully agentic, config-driven outreach platform already exist — one where you define personas, set a monthly volume, and the system autonomously finds prospects via web search/scraping, runs cold email + drip follow-ups, A/B tests messaging, and discovers new cohorts over time? And what do the two target businesses (a Psychology Today competitor; a three-sided band/sound-tech/venue marketplace) need from such a system?

---

## 1. Executive Summary

**The product you envision does not exist as a whole, but most of its parts are commoditized.** The verdict, component by component:

| Capability | Status in market (mid-2026) |
|---|---|
| Persona-defined autonomous prospecting + cold email + drip + reply handling | **Exists, commoditized.** At least five vendors ship it (11x, Artisan Ava, AiSDR, Salesforge Agent Frank, Instantly AI Sales Agent), entry pricing $250–$900/mo, several with explicit "autopilot" modes. |
| Continuous autonomous A/B testing / self-optimization | **Mostly marketing copy.** Smartlead has "AI auto-adjust" that shifts traffic to winning variants; Artisan claims self-optimization; but no vendor exposes a verifiable closed loop (hypothesis generation → controlled experiments → grounded rollout), and reviewers consistently describe optimization as human-monitored. *Partially open gap.* |
| Autonomous discovery of NEW cohorts/personas from outcome data | **Does not exist.** Every tool requires human-defined or human-approved ICPs. AiSDR's "AI Strategist" *suggests* new prospect pools; Clay/lemlist generate lookalikes on request; nobody closes the loop from outcomes back to autonomous persona expansion. **This is the genuine whitespace.** (Verified adversarially: 3 independent counterexample hunts found nothing.) |
| Targeting marketplace supply acquisition (therapists, venues, bands — not B2B SaaS buyers) | **Unserved vertical.** All AI SDRs assume prospects live in 250–700M-contact B2B databases (ZoomInfo-style). Solo therapists, bands, and cafes largely don't. The only evidence of agentic marketplace-supply acquisition is a custom consulting build (SVSG case study). **Second genuine whitespace.** |

**Strategic caution:** the AI SDR category has a quality-failure problem, not a feature problem. TechCrunch reported (2025-03-24) that 11x claimed customers it didn't have and a former employee alleged 70–80% customer churn ([TechCrunch](https://techcrunch.com/2025/03/24/a16z-and-benchmark-backed-11x-has-been-claiming-customers-it-doesnt-have/), verified 3/3). A hands-on 2026 test of 11x: 200 leads → 847 emails → 11 replies → 1 meeting. The moat for your internal system is **the closed feedback loop + non-B2B-database prospecting + per-vertical data adapters**, not "agentic sending."

**Build-vs-buy bottom line:** buy the plumbing (~$150–350/mo all-in at your volumes), build the brain. Detailed stack in §4.

---

## 2. Competitive Landscape: Agentic Outreach ("AI SDR") Platforms

### Tier 1 — full-stack autonomous AI SDR positioning

| Product | What's actually autonomous | Pricing | Notes |
|---|---|---|---|
| **11x.ai (Alice)** | Prospecting from 400M+ contacts (21+ providers) + live web search; multichannel sequences; reply handling; booking. "Self-improving" messaging is a marketing claim, no visible experiment framework. | ~$5,000/mo, annual lock-in (3rd-party reviews) | TechCrunch scandal (Mar 2025): fabricated customer claims, ZoomInfo legal threat, churn allegations of 70–80% (11x countered 79% retention). ([source](https://techcrunch.com/2025/03/24/a16z-and-benchmark-backed-11x-has-been-claiming-customers-it-doesnt-have/)) |
| **Artisan (Ava)** | 250–300M contact DB, intent signals, hyper-personalized email+LinkedIn, reply handling, booking. Claims "tests subject lines, optimizes send times, doubles down on what converts." Default fully-autonomous with optional approvals. ICP is user-defined at onboarding. | Entry dropped ~10x to ~$250/mo (Ava 2.0 self-serve, verified via [Artisan's launch post](https://www.artisan.co/blog/artisan-launches-ava-2-0-the-first-autonomous-ai-bdr-now-self-serve)) | Polarized reviews (3.8–3.9/5 G2); "extremely bland" messaging complaints; reviewers report 5–10 hrs/week human oversight in practice. |
| **AiSDR** | 700M+ lead DB + live web search; full BDR workflow; "AI Strategist" generates campaign/ICP ideas and suggests new prospect pools when engagement dips — **human approves**. | $900/mo (1,200 leads), quarterly billing ([aisdr.com](https://aisdr.com/), verified) | Closest to *assisted* cohort discovery. |
| **Salesforge (Agent Frank)** | Continuous prospecting from 500M+ DB, sends, follows up, replies, books — explicit Auto-Pilot vs Co-Pilot modes. No autonomous A/B testing; no buying-signal awareness. | From $499/mo per 1,000 active contacts ([salesforge.ai](https://www.salesforge.ai/agent/frank), verified) | |
| **Instantly.ai (AI Sales Agent)** | Finds ICP-matching leads, enriches, writes, follows up, books; Reply Agent has Autopilot mode; Deliverability Agent. | Platform from $47/mo ([instantly.ai/ai-agents](https://instantly.ai/ai-agents)) | Strong budget option. |

### Tier 2 — agent features, not end-to-end

- **Clay (Claygent)** — best-in-class *live web research* agent (browse, navigate, extract via natural language) but a human-driven workbench, not an autonomous campaign runner. Expensive at low volume ($0.14–$0.67+/lead vs ~$0.02–0.06 hitting finder APIs directly; top-up credits +50%; failed lookups still bill).
- **Reply.io (Jason AI)** — autonomous add-on, est. $500–800/mo on top of Reply.
- **Regie.ai** — explicitly co-pilot positioning, ~$35K/yr.
- **Apollo.io** — AI Assistant is draft-and-approve, not autonomous. $49–119/user/mo.
- **Unify** — signal-triggered "Plays" within a human-defined TAM, from $700/mo.
- **lemlist** — AI assists (can generate ICP personas from your website) but no autonomous reply/booking; per-seat pricing.
- **Open source** — only small experiments (open-sdr, brightdata/ai-sdr-bdr-agent, agentuity/agent-sdr). Nothing production-grade with sending infra, optimization, or persona discovery.

### Verification notes on the whitespace claims

- *"No autonomous cohort discovery"* — *Confirmed* by 3 adversarial counterexample hunts, with softening: several tools now **auto-propose** ICPs from past wins/lookalikes (AiSDR Strategist, Unify lookalikes, Warmly behavioral ICP marketing), but all are propose-then-approve. None closes the loop from reply/conversion outcomes to autonomous cohort expansion.
- *"No autonomous experimentation"* — *Confirmed in strong form, fragile in weak form*: Smartlead's "AI auto-adjust" automatically shifts traffic to better-performing variants (explicitly *without* waiting for statistical significance), and Jeeva AI claims auto-A/B. No vendor demonstrates hypothesis generation + controlled experiments + grounded rollout.
- *"No marketplace-supply vertical"* — *Confirmed (qualified, universal negative)*: only generic B2B SDRs that could be repurposed, post-acquisition onboarding tools (Mirakl Nexus), and one bespoke consulting build ([SVSG marketplace brokerage case study](https://svsg.co/resources/case-study/automating-supply-operations-in-an-online-marketplace-brokerage/)).

---

## 3. Compliance & Deliverability (constraints the backend must encode)

### Legal floor (US)

Cold B2B email **is legal** in the US under CAN-SPAM, with hard requirements (verified against FTC guidance, 3/3 confirm):

1. Truthful headers and **non-deceptive subject lines** (also litigation protection: WA's CEMA spawned ~115 class actions after an Apr 2025 ruling made misleading subjects per-se violations; CA B&P §17529.5 gives a private right of action at $1,000/email).
2. **Valid physical postal address** in every email, no exceptions.
3. Clear opt-out in every message, working ≥30 days, **honored within 10 business days**; immediate automated suppression is best practice.
4. Identify the message as an ad (flexible placement).
5. Liability extends to you even when a vendor sends on your behalf. Penalty: up to **$53,088/email** (FTC, adjusted Jan 2025).

International (if ever needed): Canada CASL is effectively opt-in (implied consent only via conspicuously-published contact info + role relevance; reverse onus); EU GDPR needs a documented legitimate-interest analysis; UK PECR is opt-out for corporate subscribers.

**Vertical check:** no special restriction on B2B solicitation *of* licensed therapists — HIPAA governs providers' handling of patient data, not vendors emailing providers (verified via HHS guidance). One nuance: patient-referral marketing can implicate state fee-splitting rules on the therapist's side; flat-fee directory pricing (the Psychology Today model) is the established safe pattern.

### Mailbox-provider floor

- **Google/Yahoo (since Feb 2024):** bulk senders (5K+/day) need SPF+DKIM+DMARC, aligned From domain, RFC 8058 one-click unsubscribe honored ≤2 days, spam complaints **<0.10% target / 0.30% hard ceiling** ([Google sender guidelines](https://support.google.com/a/answer/81126), verified by fetch).
- **Microsoft Outlook (since May 5, 2025):** SPF+DKIM+aligned DMARC for 5K+/day domains or mail is junked then rejected (`550 5.7.515`).

### Cold-email architecture consensus (2025–26 practitioner sources)

- Never send cold from the primary domain; use 2–5 lookalike secondary domains so a burned domain doesn't kill transactional email. Rebuilding a burned domain takes ~6–8 weeks; usually cheaper to abandon.
- **20–50 cold emails/inbox/day** ceiling (post-late-2025 Google Workspace crackdown, conservative operators run 15–25); warm up new inboxes **2–6 weeks** starting 10–20/day. Scale horizontally (more inboxes), never vertically.
- Verify every address pre-send (<2–3% bounce); disable open-tracking pixels (Smartlead's 14.3B-send dataset associates tracking with materially worse placement and replies).
- **Transactional ESPs (SendGrid, Mailgun, SES, Resend) ban cold email in their AUPs** — affirmative, non-transferable consent required; suspension kills your transactional sending too (verified across all four AUPs). Use cold-email-specific senders (Smartlead/Instantly).
- Mass-similar AI content is a fingerprinting risk; per-prospect personalization is both a deliverability and a reply-rate lever.

### Benchmarks (planning numbers)

| Metric | Value | Source |
|---|---|---|
| Avg cold reply rate (2024 data) | **5.8%** (down from 6.8% in 2023) | Belkins, 16.5M emails — verified |
| Reply with advanced personalization | **~17% vs ~7%** without (~2.4x) | Woodpecker, 20M emails — verified by fetch |
| First follow-up | **+40–49% more replies**; gains collapse after ~3 follow-ups; Belkins: complaints rise 0.5%→1.6% by email 4 | Woodpecker / Belkins |
| 1 contact/org vs 10+ | 7.8% vs 3.8% reply | Belkins |
| Inbox placement effect | >90% placement → 5.3% reply; <70% → 0.8% | Smartlead, 14.3B sends |
| Optimal length | 6–8 sentences / <200 words | Belkins |

**Statistics for the experimentation engine** (independently re-derived during verification): detecting a 5%→7% reply lift at 80% power needs **≈2,200 recipients per arm** — unattainable per-cohort at your volumes. Classic A/B significance testing is the wrong design; use **multi-armed bandits (Thompson sampling)** that shift traffic toward winners during learning, accepting weaker formal guarantees, plus hierarchical pooling of message-attribute effects across cohorts.

---

## 4. Build-vs-Buy: the Infrastructure Stack

**Recommendation: buy the plumbing, build the brain. ~$150–350/mo at hundreds-to-low-thousands of sends/month.**

| Layer | Buy | Cost | Notes |
|---|---|---|---|
| Sending, warmup, rotation, unsubscribe | **Smartlead** ($39 Base / $94 Pro — full API needs Pro or add-ons) or **Instantly** ($47 / $97) | $39–97/mo | Unlimited mailboxes both. The layer you must NOT build — DIY on SendGrid/SES gets accounts banned. |
| Domains & inboxes | 3–4 lookalike domains, 4–6 mailboxes | ~$30–80/mo | Google Workspace ~$7–8/user/mo or Smartlead SmartSenders $3.99–9/mailbox + $13–19/yr domains. At 1,500 sends/mo you need only ~4–6 warmed inboxes. |
| Scraping / agent search | **Apify** (pre-built actors incl. NPPES, Bandsintown) + **Firecrawl** (LLM-ready extraction) | $0–55/mo | Firecrawl free tier 1,000 pages/mo; Apify free $5 credit, Starter $39. Tavily/Exa for agentic search; skip SerpAPI (10–50x pricier than Serper). |
| Email finding | Direct waterfall: **Prospeo → FindyMail → Hunter** | ~$20–50/mo | ~$0.01–0.05/found email. Skip Clay (5–10x cost at this scale). Caveat: B2B enrichers are weakest on exactly these ICPs — much discovery will come from scraping prospects' own websites. |
| Verification | **MillionVerifier** (~$8/1K low tier) or bundled Smartlead credits | ~$10–20/mo | |
| Reply classification, experimentation, orchestration | **BUILD** — this is the differentiator | LLM API costs | Smartlead/Instantly webhooks + LLM classifier (90–97% accurate on clean replies, 5–15% manual-review tail). |
| Suppression | Build a master cross-campaign table you own; senders handle per-campaign unsubscribe | — | |

### Vertical data sources

**Therapists (excellent free data):**
- **NPPES NPI Registry** — free, public, full weekly-updated download + free query API; taxonomy codes isolate psychologists (103T*), counselors (101Y*), social workers (1041*), MFTs (106H*); includes practice address/phone but **not email** (verified — CareSet had to FOIA even email *domains*). Email enrichment from practice websites is the real work. ~510K behavioral/social-science providers were in NPI as of 2014; current per-taxonomy counts require downloading the file.
- **State licensing boards** — public rosters, complementary.
- **Psychology Today — do not scrape.** ToS §8.1 explicitly prohibits it; the 7Cups "shadow directory" lawsuits and the CareDash shutdown (below) make this the highest-reputational-risk move in the vertical.

**Music marketplace:**
- **Google Places API** — legit discovery of cafes/bars/breweries/wineries (live-music attributes, website, phone). Since Mar 2025: per-SKU free caps (10K Essentials / 5K Pro / 1K Enterprise calls/mo). ToS prohibits long-term caching — use for discovery, then scrape the venue's own site for contacts.
- **Indie on the Move** — purpose-built venue + booking-contact database: **Premium $6.99/mo** unlocks contacts; **Deluxe $34.99/mo** adds QuickPitch at $0.25/venue (verification corrected an earlier $9.99 figure). Cheapest "buy" in the vertical; pitching through their system also offloads consent risk.
- **Bandsintown** — public read-only API but licensed for artists/enterprises, not lead-gen; Apify scrapers exist with ToS caveats. Bandcamp artist pages often list emails directly.
- **Instagram** — *Meta v. Bright Data* (N.D. Cal. Jan 2024) held Meta's ToS doesn't bar logged-off scraping of public data (verified); operationally hard, use sparingly.

### Scraping-law posture (US, verified)

- *hiQ v. LinkedIn*: CFAA doesn't criminalize scraping public data (9th Cir.), **but** hiQ ultimately lost on breach-of-contract ($500K judgment, Dec 2022). ToS-violating scraping is a civil/contract and account-ban risk, not a criminal one.
- Rules to encode: never scrape behind a login; prefer government data, prospects' own sites, and licensed databases; treat ToS-prohibited sources (Psychology Today, Yelp) as off-limits given the verticals' reputational sensitivity.

---

## 5. Business 1: Therapist Directory (Psychology Today competitor)

### Market structure

| Player | Model | Price to therapist | Supply | Funding |
|---|---|---|---|---|
| **Psychology Today** | Flat-fee directory, consumer SEO moat (wins ~96% of therapy SERPs; ~971K weekly directory users claimed) | **$29.95/mo**, unchanged since ~2004 | ~80,000 US listings (⇒ ≥~$29M/yr directory revenue floor) | Bootstrapped |
| **Headway** | Free to join; monetizes via cut of insurance reimbursement (rate variable, exact take unverified) | $0 | **80,000+** (own site, 2026) | ~$225M raised, $2.3B val |
| **Grow Therapy** | Same model | $0 | 12–15K providers | **$150M Series D, Mar 2026, $3B val, ~$1B revenue** (verified) |
| **Alma** | Membership + billing | $125/mo | ~21K | ~$220M (Cigna/Optum Ventures) |
| **Zencare** | Curated directory | $59–98/mo + $130 setup | thousands | bootstrapped → acquired 2021 |
| **Mental Health Match** | Matching-quiz directory | $24.97/mo, 60-day free trial | small | indie |
| **TherapyDen** | Values-driven directory | Free (premium $10/mo) | mid | indie |
| **Zocdoc** | Pay-per-booking | $35–110/new-patient booking | multi-specialty | late-stage |

### Key insights

1. **Timing tailwind:** PT referrals are visibly drying up — documented therapist reports of inquiries falling from ~8–15/mo (2020) to ~1–3/mo (2025–26), CAC per client up 5–10x ([ClearHealthCosts series, Dec 2025–Mar 2026](https://clearhealthcosts.com/blog/2025/12/therapists-say-psychology-today-referrals-have-dried-up-and-express-concern/), verified). Therapists are also disenchanted with VC platforms (hidden spreads — one documented Alma case: $151.74 collected vs $95 paid; 81% distrust on data privacy per PsiAN). **A transparent, cheap, individual-practitioner-only directory has a receptive, actively-complaining audience — ideal conditions for outreach.**
2. **Supply was won with economics, not directories:** Headway/Grow scaled with free joining + insurance credentialing in ~30 days (vs 90–120 solo) + guaranteed payment, plus human "practice consultant" inside sales. No public evidence cold email was anyone's core channel — your outreach engine is an untested-but-plausible wedge, not a proven playbook.
3. **The CareDash bright line (critical):** CareDash scraped NPI data into unconsented public "shadow profiles," monetized via BetterHelp referrals, got an APA cease-and-desist, and shut down Feb 1, 2023 (verified via APA's own statement). **NPI data for private outreach targeting: fine. Pre-populating public profiles without consent: company-ending.**
4. **What converts therapists:** free/cheap entry, long free trials (MHM's 60 days), referral virality (PT gives free months), concierge onboarding (Zencare's founder personally photographed early therapists). Demand-side moat (SEO) is the hard part; a challenger likely needs a non-SEO demand source (AI-assistant discovery, insurer partnerships, social).
5. **Addressable outreach universe:** roughly 200–600K US therapists depending on definition (BLS, APA, market.us figures); NPPES + state boards give name/address/phone free; email coverage is the bottleneck your enrichment pipeline solves.

---

## 6. Business 2: Three-Sided Music Marketplace (bands + sound techs + venues)

### Market structure

| Player | Sides | Model | Takeaway |
|---|---|---|---|
| **GigFinesse** | artists ↔ venues (cafes, breweries, wineries, hotels) | **Free both sides**; revenue via venue partnerships; in-house production ops | **Most direct competitor**: same venue ICP, $9.4M raised ($5.8M Series A, Jun 2024, ~$40M pre). Its "production" is internal ops, not a crew marketplace. Free-to-venue sets your price anchor. |
| **Gigmor** | bands ↔ venues | Free basic; 50K+ artists | Closest legacy player; never cracked monetization (founded 2010). |
| **Indie on the Move** | bands → venue directory | $6.99/mo Premium; $34.99 Deluxe + $0.25/venue pitches | **Proof bands pay ~$7–35/mo for venue contact data** — validates the outreach-data angle standalone. |
| **GigSalad** | performers ↔ private-event bookers | Tiered membership + **2.5% fee paid members / 5% free** (verification corrected "5% flat"); 110K+ acts | Private-event focused, weak on recurring venue gigs. |
| **The Bash** | performers ↔ event bookers | $129–219/yr + 5% fee ($20 min); bid vs ~10 competitors | 1.9/5 review score; price-shopping dynamics — a wedge for you. |
| **Sofar Sounds** | curated pop-ups | Pays ~$100/25-min set vs $1,100–1,600 gross; $460K NY DOL settlement (2020, verified) | Cautionary tale: artist-hostile economics → lasting brand damage. |
| **Prism.fm / Opendate / Venuepilot** | venue-side SaaS | Subscriptions; Opendate $14M Series A (2025) | They serve venues with full-time talent buyers — your cafe/brewery ICP is below their floor. |
| **Sound-tech side** | — | Fiverr/Upwork (unspecialized), SoundBetter (studio-skewed), GigSalad à la carte ($300–900/event marketplace rate), job boards | **No structured live-sound gig marketplace exists** (verified across 6+ query angles; nearest is pre-launch DJ app SpotlightZ which name-drops engineers in a footer). |

### Key insights

1. **No three-sided band+crew+venue marketplace exists** (verified, universal-negative caveat). The sound-tech leg is genuine whitespace — and simultaneously **your riskiest assumption**: cafes paying $300–650/band often don't budget another $150+ for a tech (many use the band's PA). Marketplace rates for hired techs ($300–900/event per GigSalad) exceed informal house rates ($50–150/night). The tech leg may work best as a venue upsell (brewery weekend series, rooms with installed PAs) or churn-reduction glue rather than standalone revenue. **Test this leg first.**
2. **Sizing:** ~14.7K dedicated US live-music venues (Rentech — directional, low-credibility source) but the true TAM of "rooms that could host music" (bars/cafes/breweries/wineries) is plausibly 3–5x; ~170K BLS musician jobs (excludes most self-employed giggers, so realistically several hundred thousand working musicians); ~146K broadcast/sound/video techs (BLS, verified). US live music market ~$18.5B (2025).
3. **Economics force membership pricing:** at $300–650 GMV/gig, commission take is $30–100/booking — what starved Gigmor. Anchors: bands pay $7–35/mo for *data/access* (IOTM), venues are anchored at free (GigFinesse). The user's instinct for a membership model matches the evidence.
4. **Failure lessons:** Sonicbids (pay-per-submission "hope tax," users fled, sold off 2024), Jukely (consumer subscription, broken activation), Sofar (artist-hostile split → regulator fine). Rule: **charge the side with clear ROI; keep artist economics visibly fair; never charge for hope.**
5. **Cold-start:** Indie on the Move proved a *directory* has standalone value at zero liquidity — your outreach engine's prospect database (venues with live music, active local bands, freelance techs) is itself the cold-start asset.

---

## 7. Implications for the Unified Backend

1. **The unified backend is the right call.** Both businesses need the identical loop: define persona → discover prospects from vertical sources → enrich/find email → personalize → send + drip → classify replies → learn → expand cohorts. Only the source adapters, persona configs, and conversion goals differ.
2. **Differentiate where the market is empty:** (a) vertical source adapters for prospects who aren't in B2B databases (NPPES, Google Places, Bandsintown, artist sites), (b) a real experimentation engine (Thompson-sampling bandits + pooled message-attribute learning — not fake "AI optimization"), (c) outcome-driven cohort discovery with human-approve gates.
3. **Don't differentiate on sending.** Use Smartlead/Instantly under the hood; deliverability is a solved-but-fragile commodity where DIY is pure downside.
4. **Compliance is product, not paperwork:** suppression-first architecture, CAN-SPAM-complete templates, complaint-rate kill switches, no-scrape blocklist (Psychology Today et al.), no public shadow profiles (CareDash rule).
5. **Volume realism:** at 1,000–2,000 sends/mo across 4–6 inboxes, expect ~5–6% baseline reply rate, ~2x-able via personalization. That's ~50–200 replies/mo — enough to feed a bandit, not enough for classic A/B significance. Design the stats accordingly.
6. **Quality is the moat:** the 11x failure mode (high volume, bland messaging, churn) is the anti-pattern. Lower volume + deeper per-prospect research (the prospect's actual website, music, practice niche) is both the deliverability strategy and the conversion strategy.

## 8. Source Reliability Notes

- Confirmed by primary-source fetch: Google/Microsoft sender rules, FTC penalty figure, Smartlead/Instantly/Hunter/Firecrawl pricing, PT signup pricing & ToS, NY DOL Sofar settlement, BLS counts, TechCrunch 11x reporting, APA CareDash statement, Woodpecker/Belkins datasets.
- Directional only: Rentech venue count (no methodology), Cortexa demand-channel shares, Lavender "12x" claim (vendor hype), email-finder accuracy benchmarks (mostly competitor-published — test on your own list), Headway take-rate (variable, unverified).
- Corrected during verification: Indie on the Move pricing ($6.99 not $9.99), GigSalad fee (2.5% paid members), Headway provider count (80K+), sound-tech event rates ($300–900 marketplace vs $110–275 informal).
