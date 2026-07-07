# Makelaar Web Scraper

## Python Sidecar (20 sites)

Scrapling-based scraper in `python-sidecar/`. Catch-all for any site that doesn't expose a realtime-listings JSON feed — routing keys on that, not on Cloudflare. StealthyFetcher handles Cloudflare where present but also runs on plain-HTML sites. Writes to the same SQLite DB as the Bun app. Docker service name: `scraper-sidecar`.

Before adding new sites, read [`python-sidecar/ADDING-SITES.md`](python-sidecar/ADDING-SITES.md) — it covers Scrapling API gotchas, selector development workflow, and deployment steps.

**Huurstunt login wall**: Huurstunt hides some listings behind an email magic-link login (no password, plus reCAPTCHA — can't be automated headlessly). Anonymous locked cards link to `/aanmelden?huis=…` instead of `/huren/in/…` and only leak street+city. Set the optional `HUURSTUNT_COOKIE` env var (a logged-in browser's raw `Cookie:` header) to unlock them; the sidecar sends it on huurstunt requests and warns when the session expires (cards re-lock). The responder uses the same cookie (scoped to huurstunt only) so contact-route detection works on the now-unlocked detail pages.

## Responder

Python service in `responder/`. Docker service name: `scraper-responder`. Watches the shared SQLite DB for new listings and owns all listing Telegram notifications (letter behind a `📋 Brief` inline button) — the scrapers no longer send listing messages. Detects the contact route per makelaar (email / contact form / external); for forms it fills them in a headless browser and asks for approval via a screenshot with ✅/❌ buttons before submitting.

Key env vars: `DB_PATH`, `DATA_DIR`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`, `TELEGRAM_SALES_CHAT_IDS` (optional, see add-site flow), `TELEGRAM_ALERT_CHAT_IDS` (optional), `CONTACT_NAME`, `CONTACT_EMAIL`, `CONTACT_PHONE`, `GH_TOKEN` (optional), `GH_REPO`, `HUURSTUNT_COOKIE` (optional, see above), `POLL_INTERVAL` (default 30), `FETCH_TIMEOUT` (default 120), `RENTAL_RECHECK_INTERVAL` (default 600), `RENTAL_RECHECK_BATCH_SIZE` (default 5).

**Auto-deletion of delisted rentals** (`responder/delisting.py`) — mirrors the sales-sidecar mechanism for rentals. When a notified listing goes away (`verhuurd onder voorbehoud` / `onder optie` / `niet meer beschikbaar` / `deze woning is verhuurd`, or the detail page 404/410s), the responder deletes its original Telegram notification(s) and sends one batched summary per cycle (`🗑 N woningen niet meer beschikbaar — bericht(en) verwijderd`, replacing the previous summary). The `responses` table auto-migrates (ALTER TABLE) two responder-owned columns: `listing_status` (`available` → `gone`, tracked separately from the contact-flow `status`) and `last_checked_at` (round-robin cursor). Each `RECHECK_INTERVAL` the watcher plain-HTTP-fetches a batch of the least-recently-checked available listings; detection is deliberately conservative (a bare `verhuurd` in a "recently rented" widget must not delete a live listing) and reuses `HUURSTUNT_COOKIE` for huurstunt URLs. The Telegram 48h `deleteMessage` limit is handled gracefully — a rejected delete is logged and the listing is marked gone anyway. Env vars: `RENTAL_RECHECK_INTERVAL` (default 600), `RENTAL_RECHECK_BATCH_SIZE` (default 5).

**Add-site flow**: sending a listing URL from an unsupported site to the Telegram bot makes the responder open a GitHub issue `Add site: {domain}` (label `add-site`). The `claude-add-site.yml` workflow then runs Claude Code to add the parser per `ADDING-SITES.md` and open a PR, commenting on the issue. Requires repo secret `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`). A duplicate-issue guard returns the existing open issue instead of queueing a second one for the same domain. **Sales variant**: a URL sent from a chat in `TELEGRAM_SALES_CHAT_IDS` (comma-separated koop group) instead opens an issue labelled `add-site` + `sales` whose body targets the `sales-sidecar/` koop scraper (Delft, ≤ €270.000, ≥ 2 kamers, junk filter, kamers-vs-slaapkamers normalization); the workflow branches on the `sales` label to the sales prompt. Sales chats never receive rental listing notifications or contact-form flows, and rental chats never open sales issues.

## Sales Sidecar (koop in Delft)

Standalone Scrapling scraper in `sales-sidecar/`. Docker service name: `sales-scraper`. Scrapes apartments **for sale** (koop) in the city of Delft priced ≤ €270.000 with ≥ 2 kamers across 12 sources and notifies a dedicated Telegram group directly (no responder involvement). Two fetch paths:

- **`SITES`** (StealthyFetcher → parser): Funda koop, Pararius koop — Cloudflare / heavy-JS pages.
- **`CUSTOM_SITES`** (plain HTTP, `(existing_urls) -> list[house]`): Van Daal & Björnd (realtime-listings JSON feed, filtered to `isSales` + `statusOrig == "available"`); ZO Makelaars, VW Makelaars, Roepman, MORRIS, Hof van Delft (RealWorks list pages — plain HTTP returns the full server-rendered DOM, which also fixes ZO whose StealthyFetcher-rendered cards collapse to empty "Bewaar deze woning" widgets); Prinsenstad Makelaardij (Hayweb `sitemap_listings_res_sale.xml` → per-listing detail, skipping Verkocht / Onder bod); Olsthoorn Makelaars (custom WordPress "Sure" plugin `/wonen/` grid, paginated until an empty page, city filtered client-side); Van Silfhout Makelaars (WordPress + FacetWP `/woningaanbod/` grid, paginated via FacetWP's `wp-json/facetwp/v1/refresh` REST endpoint with facets pinned to `status=te-koop` + `locaties=delft` so filtering happens server-side).

Fetch infra (retry-once + self-restart watchdog) and the StealthyFetcher parsers are ported from `python-sidecar/scraper.py`; notification helpers from `responder/tg.py`. The RealWorks parser uses an inverted status gate (keep koop, skip `/huur/` and `/verkocht/`).

**Room semantics** — every source normalises to *total kamers* before the ≥ 2 gate: Funda cards show *slaapkamers* (bedrooms) next to a bed icon, so the parser adds the living room back (`bedrooms + 1`); the JSON feed's `rooms` field (not `bedrooms`) is total kamers; RealWorks "Aantal kamers N" (number *after* the label) is total kamers; Prinsenstad "N (waarvan M slaapkamers)" → N. So a 1-bedroom / 2-kamer flat passes and a studio / 1-kamer fails. A **junk filter** drops non-dwellings (parkeerplaats, parkeerplek, garagebox, garage, berging, bouwgrond, kavel, opslag) that slip past the price gate.

Writes to its **own** SQLite file (`data/sales.sqlite`, table `sales`, WAL) — it never touches the rental `db.sqlite`. Cross-source dedup by normalized address+city; first run seeds the table silently (no notifications); restarts never re-notify.

**Auto-deletion of sold listings** — when a listing transitions to "onder bod", "verkocht onder voorbehoud", or "verkocht", the bot deletes its Telegram notification message so the group only shows actionable listings. The `sales` table stores `tg_message_ids` (JSON list of `{chat_id, message_id}` pairs from the Telegram `sendMessage` response) and a `status` column (`available` → `sold`). Detection happens at three levels: (1) **in-cycle** — JSON feeds report `statusOrig`, Realworks URLs switch from `/koop/` to `/verkocht/`, and card-based sources (Olsthoorn, De Bruyn en Tak) surface sold badges; (2) **sitemap re-check** — each sitemap scraper (Prinsenstad, Frisia, Marloes, PSG Wonen) re-fetches up to `RECHECK_BATCH_SIZE` existing detail pages per cycle and the parser records sold URLs; (3) **universal fallback** — `recheck_available_listings()` fetches a batch of DB listings via plain HTTP and regex-matches `_SOLD_STATUS_RE` in the response body, covering Funda and any other source. The DB schema auto-migrates (ALTER TABLE) on existing deployments.

Key env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_SALES_CHAT_IDS` (comma-separated koop group), `TELEGRAM_ALERT_CHAT_IDS` (optional, falls back to sales group), `SALES_DB_PATH` (default `data/sales.sqlite`), `CHECK_INTERVAL` (default 600), `RECHECK_BATCH_SIZE` (default 5), `DEBUG_DUMP`.

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

# Watchtower
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=watchtower-logs

# System
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=system-disk
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=system-memory
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=system-processes
```
