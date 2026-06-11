# Makelaar Web Scraper

## Python Sidecar (20 sites)

Scrapling-based scraper in `python-sidecar/`. Bypasses Cloudflare via StealthyFetcher, writes to the same SQLite DB as the Bun app, sends its own Telegram notifications. Docker service name: `scraper-sidecar`.

Before adding new sites, read [`python-sidecar/ADDING-SITES.md`](python-sidecar/ADDING-SITES.md) — it covers Scrapling API gotchas, selector development workflow, and deployment steps.

## Responder

Python service in `responder/`. Docker service name: `scraper-responder`. Watches the shared SQLite DB for new listings and owns all listing Telegram notifications (letter behind a `📋 Brief` inline button) — the scrapers no longer send listing messages. Detects the contact route per makelaar (email / contact form / external); for forms it fills them in a headless browser and asks for approval via a screenshot with ✅/❌ buttons before submitting.

Key env vars: `DB_PATH`, `DATA_DIR`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`, `TELEGRAM_ALERT_CHAT_IDS` (optional), `CONTACT_NAME`, `CONTACT_EMAIL`, `CONTACT_PHONE`, `GH_TOKEN` (optional), `GH_REPO`, `POLL_INTERVAL` (default 30), `FETCH_TIMEOUT` (default 120).

**Add-site flow**: sending a listing URL from an unsupported site to the Telegram bot makes the responder open a GitHub issue `Add site: {domain}` (label `add-site`). The `claude-add-site.yml` workflow then runs Claude Code to add the parser per `ADDING-SITES.md` and open a PR, commenting on the issue. Requires repo secret `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`).

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
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=docker-stats
gh workflow run vps-exec.yml -R FrancisBehnen/Makelaar-web-scraper --ref main --field operation=docker-restart-sidecar
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
