# Responder

Watches the shared SQLite DB for listings the scrapers wrote, detects how the
makelaar wants to be contacted, and drives the respond flow from Telegram. It
is the **only** consumer of the bot's `getUpdates` — the scrapers must never
poll the same token.

## Flow per new listing

1. The watcher sees a `houses` row without a matching `responses` row.
2. `detection.py` fetches the listing page (Scrapling/StealthyFetcher, handles
   Cloudflare) and classifies the contact route:
   - `form` — a fillable contact form (message textarea + e-mail field) on the
     listing page or a linked contact page,
   - `email` — a usable e-mail address,
   - `external` — application runs via an external platform (eazlee etc.),
   - `unknown` — nothing found.
3. One Telegram notification is sent per chat with a `📋 Brief` button
   (letter on demand, keeps the feed clean) and, for forms,
   `✍️ Vul formulier in`.
4. Form flow is two-phase (`form_filler.py`):
   - **prepare**: fill the form in headless Camoufox, screenshot, store the
     exact field plan in the DB, close the browser. Nothing is submitted.
   - After ✅ in Telegram: **submit** re-applies the stored plan and clicks
     submit; the result screenshot is sent back. ❌ cancels.
   Heuristic failures and captchas downgrade to `manual` with the contact URL.
5. Everything lands in the `responses` table (`/status` in Telegram shows the
   last 15), so there is always a record of what was sent where.

Sending a listing URL of an *untracked* site to the bot offers to create a
GitHub issue (`Add site: <domain>`, label `add-site`); the `claude-add-site`
workflow then implements the parser and opens a PR.

## First deploy

On its first run with an existing DB the responder marks all current houses
as `seeded` without notifying, so the backlog isn't announced.

## Environment

| Var | Default | Purpose |
| --- | --- | --- |
| `DB_PATH` | `data/db.sqlite` | shared SQLite DB |
| `DATA_DIR` | `data` | screenshots in `DATA_DIR/screenshots` |
| `TELEGRAM_BOT_TOKEN` | — | bot token |
| `TELEGRAM_CHAT_IDS` | — | notification chats; **only** these may press buttons |
| `TELEGRAM_ALERT_CHAT_IDS` | falls back to chat ids | operational alerts |
| `CONTACT_NAME` / `CONTACT_EMAIL` / `CONTACT_PHONE` | — | form-fill personal details |
| `GH_TOKEN` | — | PAT (Issues: write) for the add-site flow; optional |
| `GH_REPO` | `FrancisBehnen/Makelaar-web-scraper` | issue target |
| `POLL_INTERVAL` | `30` | DB poll seconds |
| `FETCH_TIMEOUT` | `120` | page fetch/navigation timeout seconds |

## Known limitations

- Forms that only appear after JavaScript interaction (e.g. behind a
  "Reageer" button that swaps in a form) usually end up `manual`.
- Captchas are never solved — you get the contact URL instead.
- The submit-result screenshot is the only success signal; check it.
