# Makelaar Web Scraper

## Python Sidecar (20 sites)

Scrapling-based scraper in `python-sidecar/`. Catch-all for any site that doesn't expose a realtime-listings JSON feed — routing keys on that, not on Cloudflare. StealthyFetcher handles Cloudflare where present but also runs on plain-HTML sites. Writes to the same SQLite DB as the Bun app. Docker service name: `scraper-sidecar`.

Before adding new sites, read [`python-sidecar/ADDING-SITES.md`](python-sidecar/ADDING-SITES.md) — it covers Scrapling API gotchas, selector development workflow, and deployment steps.

**Huurstunt login wall**: Huurstunt hides some listings behind an email magic-link login (no password, plus reCAPTCHA — can't be automated headlessly). Anonymous locked cards link to `/aanmelden?huis=…` instead of `/huren/in/…` and only leak street+city. Set the optional `HUURSTUNT_COOKIE` env var (a logged-in browser's raw `Cookie:` header) to unlock them; the sidecar sends it on huurstunt requests and warns when the session expires (cards re-lock). The responder uses the same cookie (scoped to huurstunt only) so contact-route detection works on the now-unlocked detail pages.

## Shared Package (`shared/`)

Repo-root Python package imported by **both** the responder and the sales-sidecar (the Bun app and python-sidecar don't use it). Holds the Telegram + listing-lifecycle machinery that converged across the two services:

- **`shared/tg.py`** — `escape_html`, the status-button keyboard (`STATUS_BUTTONS`/`status_button_row`/`status_keyboard`, `st:r/i/x/d` — identical JSON in both services so the responder's stateless callback handler works on koop messages too), and `TelegramClient`: a `requests`-based Bot API client (`send_message`/`send_photo`/`edit_text`/`delete_message` (48h-tolerant)/`answer_callback`/`set_reaction`/`get_updates`). `requests` is imported lazily inside `_call`, so importing the module for just the pure helpers never needs `requests` — the sales-sidecar imports the helpers but keeps its own `urllib` sender for behaviour/test parity. The responder's `tg.py` binds one client to its bot token and adds the chat-scoped `broadcast`/`broadcast_photo`/`send_alert` wrappers.
- **`shared/lifecycle.py`** — the notify → track message-ids → recheck-batch → status-transition → delete + accumulating-summary lifecycle, parameterised by DB accessors, status vocabulary (verkocht/onder bod vs verhuurd/onder optie), fetch hook, and summary text. `reads_gone`/`is_gone` implement the page-scoped, conservative gone/sold detection (strip `<script>`/`<style>`/`<noscript>`/`<template>` blocks — SPA bundles like vbtverhuurmakelaars.nl embed an i18n string table with a `propertyNotFound:"…niet meer beschikbaar"` phrase on *every* page, which would otherwise mark every live listing gone — then strip sidebar/footer carousels; trust unambiguous page-status phrases anywhere; trust bare status badges only inside the `<h1>` header region — a window spanning a short `lookback` *before* the h1 (default 500 chars; some sites, e.g. Funda koop, render the "Verkocht onder voorbehoud" / "Onder bod" badge just *above* the address heading) through a longer window *after* it; 404/410 counts as gone). `run_recheck` is the round-robin loop (cursor advanced *before* the fetch; takes an `on_error` hook so recheck fetch/parse failures are logged, not silently swallowed) returning `(address, url)` pairs. `build_summary_text` renders bullets linking each (HTML-escaped) address to its listing URL (`• <a href="URL">address</a>`, `parse_mode=HTML` + `disable_web_page_preview`). `upsert_accumulating_summary` maintains **one persistent summary message per local day, edited in place** (`editMessageText` — silent, no push) as more listings go gone/sold; it accumulates entries (dedup by URL), persists a state blob (`message_ids`+`entries`+`created_at`+`day`) via injected `load_state`/`save_state` (a `kv` row in each service's DB), and only starts a fresh message (leaving the old as history) when the day changes, the summary is older than `roll_after_hours` (47), or it would exceed `max_entries` (40) / `max_chars` (3800). The send is silent so **push notifications are reserved purely for genuinely new listings**. (The older `send_replaceable_summary` remains for reference but is no longer wired up.)

**Docker / imports**: the responder and sales images build from **repo-root context** (`deploy-image.yml` `context: .`, dockerfiles `COPY responder/…`/`COPY sales-sidecar/…` + `COPY shared ./shared`) so `shared/` lands beside the entrypoint at `/app/shared`; the service runs `python -u <script>.py` from `/app`, whose script dir is on `sys.path`, making `import shared.*` resolve. Local pytest mirrors this via each suite's `conftest.py` inserting the repo root on `sys.path`. The shared modules have their own tests under `shared/tests/`.

## Responder

Python service in `responder/`. Docker service name: `scraper-responder`. Watches the shared SQLite DB for new listings and owns all listing Telegram notifications (letter behind a `📋 Brief` inline button) — the scrapers no longer send listing messages. Detects the contact route per makelaar (email / contact form / external); for forms it fills them in a headless browser and asks for approval via a screenshot with ✅/❌ buttons before submitting.

**Listing status buttons** — every listing notification (rental *and* koop) carries a one-row status keyboard below the `📋 Brief`/fill row: ✅ gereageerd, 📅 uitgenodigd, ❌ afgewezen, 🗑 niet interessant. The responder is the bot's only `getUpdates` consumer and dispatches these **statelessly** (chat_id + message_id come from the callback query), so buttons on koop messages the sales-sidecar sent are handled here too. ✅/📅/❌ set a bot reaction via `setMessageReaction` (`✍`/`🤝`/`👎` — Telegram's fixed bot-allowed set; one reaction per message replaces the previous, matching one-status-at-a-time), falling back to prefixing the message text (`✅`/`📅`/`❌`, previous prefix stripped) when reactions are disabled. 🗑 deletes the message; for rentals it also marks the listing `dismissed` (`db.mark_dismissed_by_message`, excluded from the delisting recheck so it's never re-deleted); koop 🗑 is stateless (delete + answer — the responder has no access to `sales.sqlite`). Callbacks use compact `st:r`/`st:i`/`st:x`/`st:d` codes; unknown chats are answered and ignored.

Key env vars: `DB_PATH`, `DATA_DIR`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`, `TELEGRAM_SALES_CHAT_IDS` (optional, see add-site flow), `TELEGRAM_ALERT_CHAT_IDS` (optional), `CONTACT_NAME`, `CONTACT_EMAIL`, `CONTACT_PHONE`, `GH_TOKEN` (optional), `GH_REPO`, `HUURSTUNT_COOKIE` (optional, see above), `POLL_INTERVAL` (default 30), `FETCH_TIMEOUT` (default 120), `RENTAL_RECHECK_INTERVAL` (default 600), `RENTAL_RECHECK_BATCH_SIZE` (default 5).

**Auto-deletion of delisted rentals** (`responder/delisting.py`, built on `shared/lifecycle.py`) — mirrors the sales-sidecar mechanism for rentals (both now share the same lifecycle engine). When a notified listing goes away (`verhuurd onder voorbehoud` / `onder optie` / `niet meer beschikbaar` / `deze woning is verhuurd`, or the detail page 404/410s), the responder deletes its original Telegram notification(s) and maintains one **accumulating, edited-in-place** summary per local day (`🗑 N woningen niet meer beschikbaar — bericht(en) verwijderd`) via `shared/lifecycle.upsert_accumulating_summary` — the summary state blob lives in the `kv` table under `gone_summary_ids`, the send is silent (no push), and only genuinely new listings notify. The `responses` table auto-migrates (ALTER TABLE) two responder-owned columns: `listing_status` (`available` → `gone`, tracked separately from the contact-flow `status`) and `last_checked_at` (round-robin cursor). Each `RECHECK_INTERVAL` the watcher plain-HTTP-fetches a batch of the least-recently-checked available listings; detection is deliberately conservative (a bare `verhuurd` in a "recently rented" widget must not delete a live listing) and reuses `HUURSTUNT_COOKIE` for huurstunt URLs. The Telegram 48h `deleteMessage` limit is handled gracefully — a rejected delete is logged and the listing is marked gone anyway. Env vars: `RENTAL_RECHECK_INTERVAL` (default 600), `RENTAL_RECHECK_BATCH_SIZE` (default 5).

**Telegram issue-report intake** (`chat_log` table): the responder is the bot's sole `getUpdates` consumer, so every free-text group message from a configured rental/sales chat that is *not* consumed by an existing flow (skips listing-URL submissions, `/`-commands, callbacks, and bot messages) is stored in an auto-migrated `chat_log` table (chat_id, message_id, sender name/username, ISO timestamp, text). The write is wrapped defensively so it never breaks the update loop. On startup the responder purges `chat_log` rows older than 14 days. The daily end-of-day maintenance agent reads the last 24h via the `telegram-chat-log` vps-exec operation (docker exec on `scraper-responder`).

**Add-site flow**: sending a listing URL from an unsupported site to the Telegram bot makes the responder open a GitHub issue `Add site: {domain}` (label `add-site`). The `claude-add-site.yml` workflow then runs Claude Code to add the parser per `ADDING-SITES.md` and open a PR, commenting on the issue. Requires repo secret `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`). A duplicate-issue guard returns the existing open issue instead of queueing a second one for the same domain. **Sales variant**: a URL sent from a chat in `TELEGRAM_SALES_CHAT_IDS` (comma-separated koop group) instead opens an issue labelled `add-site` + `sales` whose body targets the `sales-sidecar/` koop scraper (Delft, ≤ €270.000, ≥ 2 kamers, junk filter, kamers-vs-slaapkamers normalization); the workflow branches on the `sales` label to the sales prompt. Sales chats never receive rental listing notifications or contact-form flows, and rental chats never open sales issues.

## Sales Sidecar (koop in Delft)

Standalone Scrapling scraper in `sales-sidecar/`. Docker service name: `sales-scraper`. Scrapes apartments **for sale** (koop) in the city of Delft priced ≤ €270.000 with ≥ 2 kamers across 12 sources and notifies a dedicated Telegram group directly (no responder involvement). Two fetch paths:

- **`SITES`** (StealthyFetcher → parser): Funda koop, Pararius koop — Cloudflare / heavy-JS pages.
- **`CUSTOM_SITES`** (plain HTTP, `(existing_urls) -> list[house]`): Van Daal & Björnd (realtime-listings JSON feed, filtered to `isSales` + `statusOrig == "available"`); ZO Makelaars, VW Makelaars, Roepman, MORRIS, Hof van Delft (RealWorks list pages — plain HTTP returns the full server-rendered DOM, which also fixes ZO whose StealthyFetcher-rendered cards collapse to empty "Bewaar deze woning" widgets); Prinsenstad Makelaardij (Hayweb `sitemap_listings_res_sale.xml` → per-listing detail, skipping Verkocht / Onder bod); Olsthoorn Makelaars (custom WordPress "Sure" plugin `/wonen/` grid, paginated until an empty page, city filtered client-side); Van Silfhout Makelaars (WordPress + FacetWP `/woningaanbod/` grid, paginated via FacetWP's `wp-json/facetwp/v1/refresh` REST endpoint with facets pinned to `status=te-koop` + `locaties=delft` so filtering happens server-side); Frisia Makelaars, Marloes Makelaars, PSG Wonen (sitemap → per-listing detail, same pattern as Prinsenstad); VanHuyse Makelaars (WordPress + WP-Realworks `realworks_wonen-sitemap*.xml`, discovered from `sitemap_index.xml` since Yoast chunks it at 1000 URLs/file — candidates filtered on the `/koop/.../delft/` URL path segments before any detail fetch; the site's WAF 403s the shared Chrome/120 UA the other sources use, so this source fetches with its own Firefox UA (`_vanhuyse_get`), isolated from `_http_get` so no other source is affected).

Fetch infra (retry-once + self-restart watchdog) and the StealthyFetcher parsers are ported from `python-sidecar/scraper.py`; the Telegram helpers (`escape_html`, status-button keyboard) and the sold-listing lifecycle come from the repo-root **`shared/`** package (shared with the responder — see the Shared Package section), though the live `_send`/`_delete_message` senders stay local (`urllib`). The RealWorks parser uses an inverted status gate (keep koop, skip `/huur/` and `/verkocht/`).

**Room semantics** — every source normalises to *total kamers* before the ≥ 2 gate: Funda cards show *slaapkamers* (bedrooms) next to a bed icon, so the parser adds the living room back (`bedrooms + 1`); the JSON feed's `rooms` field (not `bedrooms`) is total kamers; RealWorks "Aantal kamers N" (number *after* the label) is total kamers; Prinsenstad "N (waarvan M slaapkamers)" → N. So a 1-bedroom / 2-kamer flat passes and a studio / 1-kamer fails. A **junk filter** drops non-dwellings (parkeerplaats, parkeerplek, garagebox, garage, berging, bouwgrond, kavel, opslag) that slip past the price gate.

Writes to its **own** SQLite file (`data/sales.sqlite`, table `sales`, WAL) — it never touches the rental `db.sqlite`. Cross-source dedup by normalized address + **city token** (`find_duplicate`/`_city_token` strips a leading `NNNN XX` postcode and any `(neighbourhood)` suffix so Funda's `2624 DJ Delft` and Pararius's `2624 DK Delft (Voorhof-Hoogbouw)` collapse to `delft` and dedup to one notification); first run seeds the table silently (no notifications); restarts never re-notify.

**Listing status buttons** — each koop notification attaches the same one-row status keyboard as the responder (`_status_button_row` in `sales_scraper.py`: ✅/📅/❌/🗑 → `st:r`/`st:i`/`st:x`/`st:d`), kept JSON-identical so the responder's stateless callback handler works on these messages even though the sales-sidecar never polls `getUpdates`. Only listing `sendMessage`s get the keyboard (alerts/summaries don't). A 🗑-dismissed message the responder deletes is tolerated by the sales-sidecar's own later sold-deletion attempt (`_delete_message` logs-and-continues on the already-gone message).

**Auto-deletion of sold listings** — when a listing transitions to "onder bod", "verkocht onder voorbehoud", or "verkocht", the bot deletes its Telegram notification message so the group only shows actionable listings. The `sales` table stores `tg_message_ids` (JSON list of `{chat_id, message_id}` pairs from the Telegram `sendMessage` response) and a `status` column (`available` → `sold`). Detection happens at three levels: (1) **in-cycle** — JSON feeds report `statusOrig`, Realworks URLs switch from `/koop/` to `/verkocht/`, and card-based sources (Olsthoorn, De Bruyn en Tak) surface sold badges; (2) **sitemap re-check** — each sitemap scraper (Prinsenstad, Frisia, Marloes, PSG Wonen, VanHuyse) re-fetches up to `RECHECK_BATCH_SIZE` existing detail pages per cycle and the parser records sold URLs (VanHuyse's own re-check is also the *only* sold-detection path for that source — the universal fallback below 403s on it, per its custom-UA note above); (3) **universal fallback** — `recheck_available_listings()` fetches a batch of DB listings via plain HTTP and applies the shared page-scoped detection (`shared.lifecycle.is_gone`), covering Funda and any other source. This adopts the responder's better variant: round-robin recheck (`ORDER BY last_checked_at ASC`, cursor advanced before the fetch via a new `last_checked_at` column) supersedes the old fixed `rowid ASC` + whole-body `_SOLD_STATUS_RE`, and detection is now conservative (strips `<script>`/`<style>`/`<noscript>`/`<template>` and sidebar/footer carousels, trusts bare "verkocht"/"onder bod" badges only in the `<h1>` header region, and treats 404/410 as sold) instead of a whole-body regex. The in-cycle detection paths (levels 1 & 2 above) still feed `_record_sold_url`/`process_sold_urls` into the same summary. Funda/Pararius rechecks route through StealthyFetcher with a longer `RECHECK_FETCH_TIMEOUT` (default 240s — Cloudflare solve regularly overruns the 120s scrape-cycle `FETCH_TIMEOUT`); `run_recheck`'s `on_error` hook logs recheck failures instead of silently advancing the cursor. **Cross-source status reconciliation** (`_sold_siblings`, `reconcile_cross_source`, `_sold_reason`, `sold_reason` column) — policy: *an explicit sold on any source is authoritative for the whole property; a mere disappearance is not.* `_sold_reason(url)` classifies a page as `explicit` (a positive Verkocht/Onder bod status via `lifecycle.reads_gone`), `gone` (404/410), or `None` (live); the `sales.sold_reason` column records why each row went sold (ALTER-TABLE auto-migrated; in-cycle feed/RealWorks/badge detections + Funda/Pararius badge rechecks are `explicit`, bare 404s are `gone`). When a listing goes sold, same-address + city-token siblings still `available` are propagated to: **unconditionally deleted if the primary reason is `explicit`** (the property is sold — lagging portals like a slow Pararius are just behind, so their stale card is removed without waiting), but only deleted **if the sibling's own page also reads sold when the primary merely `gone`** (guards the Westvest case — a makelaar withdrawing a duplicate while the flat is still live elsewhere). `reconcile_cross_source` runs each cycle to heal *standing* lag (an `available` row whose sibling already went `explicit`-sold in an earlier cycle), with a bounded one-refetch backfill (`RECHECK_BATCH_SIZE` cap) for legacy NULL-`sold_reason` rows. The sold **summary** is the shared `upsert_accumulating_summary` (one edited-in-place, silent, per-day message; state in a `kv` table added to `sales.sqlite`). The DB schema auto-migrates (ALTER TABLE — `tg_message_ids`, `status`, `last_checked_at`, `sold_reason`; `CREATE TABLE IF NOT EXISTS kv`) on existing deployments.

Key env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_SALES_CHAT_IDS` (comma-separated koop group), `TELEGRAM_ALERT_CHAT_IDS` (optional, falls back to sales group), `SALES_DB_PATH` (default `data/sales.sqlite`), `CHECK_INTERVAL` (default 600), `RECHECK_BATCH_SIZE` (default 5), `RECHECK_FETCH_TIMEOUT` (default 240, stealthy-recheck timeout for Funda/Pararius), `DEBUG_DUMP`.

## Hostinger VPS

Deployment target for this project.

### SSH Access
- **Alias**: `ssh hostinger` (configured in `~/.ssh/config`)
- **Host**: srv1407177.hstgr.cloud (187.77.93.210)
- **User**: francisbehnen (passwordless sudo via `/etc/sudoers.d/francisbehnen`)
- **Key**: `~/.ssh/id_ed25519_hostinger`
- Root SSH and password auth are disabled.

> **Sandbox note**: SSH requires `dangerouslyDisableSandbox: true` because port 22 is blocked by the default sandbox even though the host is whitelisted.

### Common Local Commands
```bash
# SSH in
ssh hostinger

# Check running containers
ssh hostinger 'sudo docker ps'

# View docker-compose logs
ssh hostinger 'sudo docker logs --tail 50 <container-name>'
```

## Checking a Makelaar's Live Feed

To verify whether a listing appears in a site's JSON feed, use the `WebFetch` tool to directly fetch the feed URL (e.g. `https://www.verra.nl/nl/realtime-listings/consumer`) — no workflow needed. For HTML-only sites scraped by the python sidecar, fetch the listing page directly the same way.

## Cloud Maintenance (Claude Code Web/Mobile)

When running in the cloud (claude.ai/code), direct SSH is unavailable due to sandbox
proxy limitations. Use GitHub Actions workflows instead.

### Prerequisites
- `GH_TOKEN` must be set as a Custom Environment variable in Claude Code web settings.
- Always use `-R FrancisBehnen/Makelaar-web-scraper` flag with `gh` commands.
- Always pass `--ref main` to `gh workflow run` (proxy can't resolve default branch).
- To read workflow output: use `gh run list` then `gh api repos/FrancisBehnen/Makelaar-web-scraper/actions/runs/<id>/jobs` (direct `gh run view --log` returns 403 through the proxy).
- If the `gh` CLI is unavailable, dispatch workflows directly via the GitHub REST API: `curl -X POST -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/repos/FrancisBehnen/Makelaar-web-scraper/actions/workflows/<file>.yml/dispatches -d '{"ref":"main","inputs":{...}}'` (and fetch run logs from `.../actions/runs/<id>/logs`).

### Quick Workflows

```bash
# Health check (containers, errors, disk, memory)
gh workflow run scraper-health.yml -R FrancisBehnen/Makelaar-web-scraper --ref main

# Fetch logs (default: sidecar, 50 lines)
gh workflow run scraper-logs.yml -R FrancisBehnen/Makelaar-web-scraper --ref main
gh workflow run scraper-logs.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field container=makelaar-scraper --field lines=200

# Restart container
gh workflow run scraper-restart.yml -R FrancisBehnen/Makelaar-web-scraper --ref main
gh workflow run scraper-restart.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field container=both
```

### VPS Command Dispatcher

All operations via `vps-exec.yml`:

```bash
# Docker
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=docker-ps
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=docker-logs-sidecar --field lines=200
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=docker-logs-errors --field lines=500
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=docker-logs-responder --field lines=200
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=docker-stats
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=docker-restart-sidecar
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=docker-restart-responder
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=docker-compose-config

# Scraper diagnostics
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=scraper-cycle-status
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=scraper-db-stats
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=scraper-last-new
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=responder-detection-stats
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=telegram-chat-log

# Watchtower
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=watchtower-logs

# System
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=system-disk
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=system-memory
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=system-processes
```
