# Plan: Auto-reacting to account-gated, capped-viewing listings

Status: **proposal** — no code yet. This document scopes the work and flags the
decisions that need a human call before anything is built.

## 1. The problem

We already *find* every Björnd listing — `bjornd.nl` is scraped through its
`realtime-listings/consumer` feed (`src/makelaars.ts:18`). Finding is not the
bottleneck. **Reacting fast enough is.**

A growing class of makelaars no longer take a reply on the listing page. They
gate the response behind:

1. **an account / portal reaction** — you must be logged in to Pararius, Funda
   or the makelaar's own site to press "Reageer"; there is no inbox or
   listing-page contact form, so our `detection.detect()` correctly returns
   `unknown` (this is the documented Vesteda case in
   `python-sidecar/ADDING-SITES.md`), and the current responder can do nothing
   with it; **and/or**
2. **a follow-up questionnaire** — after the initial reaction the makelaar
   e-mails a tokenised prequalification link that must be completed before a
   viewing slot is assigned.

Björnd is the worked example. Reacting via Pararius, Funda *or* bjornd.nl
triggers an e-mail (screenshots in the issue):

- First mail: *"Bedankt voor je interesse in Noordeinde 34, 2611KJ DELFT … vul
  eerst een korte vragenlijst in"* with a **Vragenlijst invullen** button →
  `https://app.housap.com/prequal/<token>`.
- If you submit it late: *"je aanvraag wordt op dit moment **niet in
  behandeling genomen omdat de bezichtigingsronde reeds is volgepland**"*.

So unlike the "online for a few hours, then a random draw" makelaars, these run
**first-come-first-served viewing slots that fill within minutes**. We are
losing that race today because:

- nothing reacts on the account-gated portals at all, and
- even where we do react, the housap questionnaire is a second gate we don't
  touch, and the whole flow waits on a human pressing ✅ in Telegram.

Goal: **complete the full reaction chain — portal reaction → questionnaire —
within seconds of the listing appearing, with no human in the critical path**,
so we land a slot before the round fills.

## 2. Anatomy of the housap gate (why a normal form-fill won't do)

- The prequal URL carries a **per-recipient token**
  (`/prequal/db9c8652…`). It is *only* reachable from the e-mail — fetching it
  unauthenticated returns **HTTP 403**. We cannot synthesize or guess it; it
  must be read out of our inbox.
- It is a hosted SPA (housap.com), almost certainly a **multi-step wizard**
  asking screening questions: household composition, gross income / income
  multiple of rent, employment, guarantor, pets, smoking, desired move-in date,
  current living situation. The existing `responder/form_filler.py` handles a
  **single** name/email/phone/message contact form on one page — it has no
  concept of steps, "Next" buttons, or income/household answer values.
- The mail correlates to a listing by the address printed in the body
  (*"Noordeinde 34, 2611KJ DELFT"*), which is how we tie it back to a
  `responses` row.

## 3. Where we lose the race (latency budget)

Current end-to-end path and its delays:

| Stage | Component | Current latency |
| --- | --- | --- |
| Listing appears → stored | scraper feed poll | scrape cycle interval |
| Stored → picked up | responder watcher | up to `POLL_INTERVAL` (30s) + cycle |
| Contact detection | `detection.detect()` (headless browser) | ~10–15s cold browser |
| Fill → **submit** | waits for human ✅ in Telegram | **minutes (the killer)** |
| Portal-account reaction | — | **not handled** |
| Questionnaire e-mail gate | — | **not handled** |

The two unhandled stages are the whole point of this work; the human-approval
wait is the single biggest fixable delay for this listing class.

## 4. Proposed architecture

Six additions, ordered by leverage. Each is independently shippable.

### 4.1 Applicant profile config (foundation, do first)

A structured, canned set of screening answers so the questionnaire can be filled
deterministically. Mirror the facts already baked into the aanmeldbrief
(`responder/letter.py`) so answers stay consistent with the letter: two
occupants, combined income (€2 000–2 500 + €3 000–3 500), employment, no
pets/non-smoker, desired move-in, etc.

- New `responder/profile.py` + env vars (`APPLICANT_*`), loaded in `config.py`.
- Express answers as both raw values and **bracket pickers** (income ranges,
  household size, yes/no) so the filler can match radio/select options.

### 4.2 Inbox watcher (new module)

Watch the `CONTACT_EMAIL` mailbox for makelaar follow-up mail and extract the
gate link.

- IMAP IDLE (push) with a polling fallback, in a new `responder/inbox.py`
  thread alongside `watcher`/`bot`/`browser-worker` in `responder.main()`.
- Match heuristics: known gate hosts (`app.housap.com/prequal/…` first), plus
  generic CTA-button/link patterns (`vragenlijst`, `reageer`, `aanvraag`,
  `bezichtiging`).
- **Correlate** the mail to a `responses` row by the address string in the body
  (reuse the address-normalisation logic in `db.find_prior_response`).
- Enqueue a `("questionnaire", response_id, gate_url)` browser job.
- Record the link/gate in a new `gate_url` / `gate_stage` column (§5).

### 4.3 Questionnaire auto-filler (extend form_filler)

Generalise `form_filler.py` from one-shot to a **multi-step wizard driver**:

- loop: collect visible fields → map via profile (§4.1) + existing role
  heuristics → fill → click Next/Volgende → repeat until a submit/finish or no
  progress;
- answer richer field types: radios, brackets, date pickers, yes/no;
- keep the existing safety bails (captcha, ambiguous property select) →
  downgrade to `manual` with the gate URL.
- Reuse the screenshot-per-step approach for the audit trail.

### 4.4 Portal-account reaction (the "requires an account" core)

For listings where detection is `unknown` because the reaction lives behind a
login (Pararius/Funda/own site):

- A **logged-in browser session per portal**, credentials in env
  (`PARARIUS_USER/PASS`, `FUNDA_*`), cookies persisted on the `scraper-data`
  volume so we re-use sessions and minimise login/captcha friction.
- A small per-portal "react" driver: open the listing, click
  Reageer/Inschrijven, fill the portal's reaction form from the profile, submit.
- Routing: add a `portal` contact method to `detection.ContactInfo`; map the
  makelaar/source to its portal reaction driver.
- This is the heaviest, most ToS-sensitive piece — see §7. Recommend shipping it
  **last**, behind a per-site allowlist, after 4.1–4.3 prove the gate flow.

### 4.5 Fast-path auto-submit (remove the human from the critical path)

For trusted, capped, account-gated sites, skip the ✅ wait:

- a per-site / global `AUTO_SUBMIT` allowlist; when set, `prepare` flows
  straight into `submit` and **then** posts the screenshot + result to Telegram
  ("✅ verstuurd, hier is wat is ingevuld") instead of asking first;
- content risk is low — the letter and profile answers are canned — but this is
  a real behaviour change, so it is **opt-in per site**, never the default;
- pair with a much shorter responder poll (or event-driven trigger on insert)
  for the capped class.

### 4.6 Instrumentation (so we know if we're actually winning)

We can shave latency forever and still lose if the cap is tiny. Measure it:

- timestamp each stage in `responses` (seen → reacted → mail-received →
  questionnaire-submitted → outcome);
- parse the **outcome mail** ("in behandeling" vs "reeds volgepland") to label
  each attempt win/loss;
- a `/stats` Telegram command surfacing median seen→submitted time and win rate
  per site. This tells us whether to invest further or accept some caps are
  unwinnable.

## 5. Data model changes

Extend the `responses` table (`responder/db.py:init_schema`) — additive, no
migration pain:

- `gate_url TEXT` — the housap/questionnaire link from the inbox watcher;
- `gate_stage TEXT` — `reacted` | `awaiting_gate_mail` | `gate_filling` |
  `gate_submitted` | `rejected_full` | `accepted`;
- stage timestamps for §4.6;
- new `STATUS_EMOJI` / status values for the gate stages.

## 6. Phased rollout

1. **Profile config (§4.1)** + data-model columns (§5). No behaviour change.
2. **Inbox watcher (§4.2)** in observe-only mode: detect housap mail, correlate,
   notify Telegram with the link — human still clicks. Validates correlation.
3. **Questionnaire filler (§4.3)** with manual ✅ approval. Validates fill
   quality on the real housap wizard via screenshots.
4. **Instrumentation (§4.6)** — start measuring before optimising.
5. **Fast-path auto-submit (§4.5)** for housap-gated sites once fill quality is
   proven. This is when we actually start beating the clock.
6. **Portal-account reaction (§4.4)**, per-site allowlist, last and most
   carefully.

## 7. Risks & open questions

- **ToS / automation**: logged-in portal automation (Pararius/Funda) and
  auto-submitting screening forms may breach platform terms and risks account
  bans / captchas. Needs an explicit human go/no-go before §4.4. Keep it
  allowlisted and easy to disable.
- **Captcha** on portal login or the housap wizard — current code bails to
  manual; same fallback applies, but it caps how fast/auto we can be.
- **Inbox access**: which mailbox, and IMAP vs an API (Gmail)? The account is
  `francis.behnen@gmail.com` per session context — Gmail needs an app password
  or OAuth. **Decision needed.**
- **Memory**: only one Camoufox runs at a time (`config.BROWSER_LOCK`). Adding
  portal sessions + questionnaire fills contends for that lock and may need a
  second browser slot or careful queueing on the VPS.
- **Diminishing returns**: if a round truly caps at a handful and fills in
  seconds, even a perfect bot may lose. §4.6 exists to decide this with data,
  not vibes.
- **housap variability**: the wizard's exact steps/fields are unverified (the
  link 403s without the token). The filler must be heuristic and screenshot-
  audited, not hard-coded to fields we're guessing at.

## 8. Decisions needed before build

1. Auto-submit without human approval for capped sites — yes (per-site
   allowlist) or keep ✅-in-the-loop?
2. Mailbox integration: Gmail API/OAuth vs plain IMAP app password?
3. Portal-account reaction (Pararius/Funda login automation) — in scope now, or
   defer until the housap gate flow is proven?
4. Where credentials/profile live (env vars vs a secrets file on the volume).
