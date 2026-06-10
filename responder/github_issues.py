"""Create 'Add site' GitHub issues; a Claude Code workflow picks them up."""

import logging
from urllib.parse import urlparse

import requests

from config import GH_REPO, GH_TOKEN

log = logging.getLogger("responder")


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def create_add_site_issue(listing_url: str) -> str:
    """Open the issue and return its html_url. Raises on failure."""
    if not GH_TOKEN:
        raise RuntimeError("GH_TOKEN is niet geconfigureerd")
    domain = domain_of(listing_url)
    body = (
        f"Add this makelaar site to the python-sidecar scraper, following "
        f"`python-sidecar/ADDING-SITES.md`.\n\n"
        f"Example listing URL: {listing_url}\n\n"
        f"Requested via the Telegram responder bot."
    )
    resp = requests.post(
        f"https://api.github.com/repos/{GH_REPO}/issues",
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "title": f"Add site: {domain}",
            "body": body,
            "labels": ["add-site"],
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        log.error("GitHub issue creation failed (%s): %s", resp.status_code, resp.text)
        raise RuntimeError(f"GitHub gaf status {resp.status_code}")
    return resp.json()["html_url"]
