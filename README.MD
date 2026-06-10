# Makelaar web scraper

Scrapes Dutch rental listings and helps you respond to them fast. Three services
share a SQLite database on a Docker volume and are deployed to a Hostinger VPS
(push to `main` → GHCR images → Watchtower pulls and restarts).

## Architecture

| Service | Code | Image | Role |
| --- | --- | --- | --- |
| `makelaar-scraper` | `src/` (Bun) | `ghcr.io/francisbehnen/makelaar-web-scraper:main` | Scrapes a set of sites, writes listings to the shared DB |
| `scraper-sidecar` | `python-sidecar/` (Python + Scrapling) | `ghcr.io/francisbehnen/makelaar-web-scraper-sidecar:main` | Scrapes ~20 Cloudflare-protected sites, writes to the same DB, sends operational alerts |
| `scraper-responder` | `responder/` (Python) | `ghcr.io/francisbehnen/makelaar-web-scraper-responder:main` | Watches the DB for new listings and handles all listing notifications and contact flows |

The scrapers no longer send listing messages themselves — the responder owns
that. The sidecar still sends operational alerts.

## Responder flows

- **Listing notifications**: every new listing in the DB triggers a Telegram
  message with inline buttons. The application letter is kept behind a
  `📋 Brief` button so the chat stays compact.
- **Contact detection**: the responder works out how to contact the makelaar —
  direct email, an on-site contact form, or an external platform.
- **Form-fill approval**: for contact forms it fills the form in a headless
  browser, takes a screenshot, and sends it to Telegram. You approve with ✅
  (submit) or reject with ❌ (abort). Nothing is submitted without approval.
- **Email copy flow**: when the makelaar exposes an email address, the
  responder sends you the address and a ready-to-send letter to copy.
- **Add a site from Telegram**: send a listing URL from an unsupported site to
  the bot. The responder creates a GitHub issue titled `Add site: {domain}`
  (label `add-site`, body contains the example URL). The
  `claude-add-site.yml` GitHub Action then has Claude Code add the parser
  following `python-sidecar/ADDING-SITES.md` and open a PR, commenting back on
  the issue.

## Setup

### VPS `.env` additions

Alongside the existing `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_IDS` (and optional
`TELEGRAM_ALERT_CHAT_IDS`), the responder needs:

```bash
CONTACT_NAME=          # your full name, used in forms and letters
CONTACT_EMAIL=         # your email address
CONTACT_PHONE=         # your phone number
GH_TOKEN=              # optional: fine-grained PAT with Issues:write on this
                       # repo, used to create add-site issues from Telegram
```

`GH_REPO` defaults to `FrancisBehnen/Makelaar-web-scraper` and only needs
setting for a fork.

### Repo secret for the add-site Action

The `claude-add-site.yml` workflow needs the repository secret
`CLAUDE_CODE_OAUTH_TOKEN`. Generate it locally with:

```bash
claude setup-token
```

and add it under the repo's Settings → Secrets and variables → Actions.

## Development

Install git hooks:

```bash
git config --add core.hooksPath .hooks
```
