# Makelaar Web Scraper

## Python Sidecar (Pararius & Funda)

Scrapling-based scraper in `python-sidecar/`. Bypasses Cloudflare via StealthyFetcher, writes to the same SQLite DB as the Bun app, sends its own Telegram notifications.

Before adding new sites, read [`python-sidecar/ADDING-SITES.md`](python-sidecar/ADDING-SITES.md) — it covers Scrapling API gotchas, selector development workflow, and deployment steps.

## Hostinger VPS

Deployment target for this project.

### SSH Access
- **Alias**: `ssh hostinger` (configured in `~/.ssh/config`)
- **Host**: srv1407177.hstgr.cloud (187.77.93.210)
- **User**: francisbehnen (passwordless sudo via `/etc/sudoers.d/francisbehnen`)
- **Key**: `~/.ssh/id_ed25519_hostinger`
- Root SSH and password auth are disabled.

> **Sandbox note**: SSH requires `dangerouslyDisableSandbox: true` because port 22 is blocked by the default sandbox even though the host is whitelisted.

### Common Commands
```bash
# SSH in
ssh hostinger

# Check running containers
ssh hostinger 'sudo docker ps'

# View docker-compose logs
ssh hostinger 'sudo docker logs --tail 50 <container-name>'
```
