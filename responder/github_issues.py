"""Create 'Add site' GitHub issues; a Claude Code workflow picks them up."""

import logging
from urllib.parse import urlparse

import requests

from config import GH_REPO, GH_TOKEN

log = logging.getLogger("responder")

_RENTAL_BODY = (
    "Add this makelaar site to the python-sidecar scraper, following "
    "`python-sidecar/ADDING-SITES.md`.\n\n"
    "Example listing URL: {url}\n\n"
    "Requested via the Telegram responder bot."
)

_SALES_BODY = (
    "Add this makelaar site to the **sales-sidecar** koop scraper "
    "(`sales-sidecar/`), NOT the rental python-sidecar.\n\n"
    "Example listing URL: {url}\n\n"
    "Target service: `sales-sidecar/sales_scraper.py` (Docker service "
    "`sales-scraper`). It scrapes apartments **for sale** (koop) in the city "
    "of **Delft**, priced **≤ €270.000** with **≥ 2 kamers**, and drops "
    "non-dwellings via a junk filter (parkeerplaats/garagebox/berging/kavel "
    "etc.).\n\n"
    "Prefer the plain-HTTP path: add the source to `CUSTOM_SITES` "
    "(`(existing_urls) -> list[house]`) when the site exposes a realtime-"
    "listings JSON feed or a server-rendered RealWorks/Hayweb DOM. Only fall "
    "back to `SITES` (StealthyFetcher → parser) for Cloudflare / heavy-JS "
    "pages.\n\n"
    "Follow the **room semantics** convention already used there: normalise "
    "every source to *total kamers* before the ≥ 2 gate — Funda cards show "
    "*slaapkamers* (bedrooms) so add the living room back (`bedrooms + 1`); "
    "the JSON feed's `rooms` field is total kamers; RealWorks \"Aantal kamers "
    "N\" is total kamers; \"N (waarvan M slaapkamers)\" → N. Apply the koop "
    "price/junk filters and add tests under `sales-sidecar/tests`.\n\n"
    "Requested via the Telegram responder bot (Huisje kopen group)."
)


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def _find_open_issue(title: str) -> str | None:
    """Return the html_url of an existing open add-site issue, or None.

    Mirrors the dedup intent: the bot should not queue a second issue for a
    domain that already has one open and waiting for the workflow.
    """
    resp = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/issues",
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        params={"state": "open", "labels": "add-site", "per_page": 100},
        timeout=30,
    )
    if resp.status_code >= 300:
        # Don't let a flaky search block issue creation; just log and continue.
        log.warning("GitHub issue lookup failed (%s): %s", resp.status_code, resp.text)
        return None
    for issue in resp.json():
        if issue.get("title") == title:
            return issue.get("html_url")
    return None


def create_add_site_issue(listing_url: str, *, sales: bool = False) -> str:
    """Open the issue and return its html_url. Raises on failure.

    When ``sales`` is True the issue is labelled ``sales`` in addition to
    ``add-site`` and its body targets the sales-sidecar koop scraper, so the
    claude-add-site workflow branches to the right service.

    If an open add-site issue for the same domain already exists, its URL is
    returned instead of opening a duplicate.
    """
    if not GH_TOKEN:
        raise RuntimeError("GH_TOKEN is niet geconfigureerd")
    domain = domain_of(listing_url)
    title = f"Add site: {domain}"

    existing = _find_open_issue(title)
    if existing:
        log.info("Add-site issue for %s already open: %s", domain, existing)
        return existing

    body_template = _SALES_BODY if sales else _RENTAL_BODY
    labels = ["add-site", "sales"] if sales else ["add-site"]
    resp = requests.post(
        f"https://api.github.com/repos/{GH_REPO}/issues",
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "title": title,
            "body": body_template.format(url=listing_url),
            "labels": labels,
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        log.error("GitHub issue creation failed (%s): %s", resp.status_code, resp.text)
        raise RuntimeError(f"GitHub gaf status {resp.status_code}")
    return resp.json()["html_url"]
