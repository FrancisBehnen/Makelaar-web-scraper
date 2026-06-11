"""Figure out how a makelaar wants to be contacted for a listing.

Detection order (most actionable first):
1. a fillable contact form on the listing page itself,
2. an e-mail address anywhere on the page,
3. a fillable form on a linked same-domain contact page (followed once),
4. an apply-link/form posting to a known external rental platform.
"""

import concurrent.futures
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from scrapling.fetchers import StealthyFetcher

from config import BROWSER_LOCK, FETCH_TIMEOUT

log = logging.getLogger("responder")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Addresses that are clearly not a makelaar inbox.
EMAIL_JUNK = (
    "noreply",
    "no-reply",
    "donotreply",
    "example.",
    "sentry",
    "wixpress",
    "@2x",
    "placeholder",
    "yourdomain",
)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")

CONTACT_LINK_RE = re.compile(r"contact|reageer|bezichtig|aanvraag", re.IGNORECASE)

# A link to an external rental platform is only the application route when it
# actually reads like one. Some sites (notably Pararius) carry sister-portal or
# footer links to these platforms that have nothing to do with responding to
# *this* listing, so a bare platform link must show apply intent to count.
APPLY_INTENT_RE = re.compile(
    r"reage|inschrij|aanmeld|solliciteer|\bapply\b|aanvraag|aanvragen|bezichtig",
    re.IGNORECASE,
)

# Rental platforms makelaars outsource their application flow to.
EXTERNAL_PLATFORMS = (
    "eazlee.com",
    "huurwoningen.nl",
    "ikwilhuren.nu",
    "woningnet.nl",
    "huurportaal.nl",
    "leadflow.rent",
)

CAPTCHA_SELECTOR = (
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], '
    'script[src*="recaptcha"], script[src*="hcaptcha"], script[src*="turnstile"], '
    '[class*="g-recaptcha"], [class*="h-captcha"], [class*="cf-turnstile"]'
)

# Forms that exist for something other than contacting the makelaar.
NON_CONTACT_FORM_RE = re.compile(
    r"search|zoek|nieuwsbrief|newsletter|login|inloggen", re.IGNORECASE
)

_fetch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)


@dataclass
class ContactInfo:
    method: str  # email | form | external | unknown
    detail: str = ""  # email address, form page URL, or external URL
    email: str = ""  # e-mail address also found when method != email
    captcha: bool = False


def _fetch(url: str):
    with BROWSER_LOCK:
        future = _fetch_pool.submit(
            StealthyFetcher.fetch,
            url,
            headless=True,
            solve_cloudflare=True,
            network_idle=True,
        )
        try:
            return future.result(timeout=FETCH_TIMEOUT)
        except Exception as exc:
            log.error("Detection fetch of %s failed: %s", url, exc)
            return None


def _registrable_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _find_email(page, listing_url: str) -> str:
    candidates: list[str] = []
    for anchor in page.css('a[href^="mailto:"]'):
        address = anchor.attrib.get("href", "")[len("mailto:") :].split("?")[0]
        if address:
            candidates.append(address.strip())
    html = page.body.decode("utf-8", errors="ignore") if page.body else ""
    candidates.extend(EMAIL_RE.findall(html))

    seen: list[str] = []
    for address in candidates:
        address = address.strip().strip(".").lower()
        if not EMAIL_RE.fullmatch(address):
            continue
        if any(junk in address for junk in EMAIL_JUNK):
            continue
        if address.endswith(IMAGE_EXTENSIONS):
            continue
        if address not in seen:
            seen.append(address)
    if not seen:
        return ""
    site_domain = _registrable_domain(listing_url)
    for address in seen:
        if address.split("@")[1] == site_domain:
            return address
    return seen[0]


def _form_is_fillable(form) -> bool:
    """A form our filler can handle: a message textarea plus an email field."""
    identity = " ".join(
        (
            form.attrib.get("id", ""),
            form.attrib.get("class", ""),
            form.attrib.get("action", ""),
            form.attrib.get("name", ""),
        )
    )
    if NON_CONTACT_FORM_RE.search(identity):
        return False
    if not form.css("textarea"):
        return False
    for field in form.css("input"):
        attrs = " ".join(
            (
                field.attrib.get("type", ""),
                field.attrib.get("name", ""),
                field.attrib.get("id", ""),
                field.attrib.get("placeholder", ""),
            )
        ).lower()
        if "mail" in attrs:
            return True
    return False


def _has_fillable_form(page) -> bool:
    return any(_form_is_fillable(form) for form in page.css("form"))


def _has_captcha(page) -> bool:
    return bool(page.css(CAPTCHA_SELECTOR))


def _find_external(page, listing_url: str) -> str:
    site_domain = _registrable_domain(listing_url)
    # A form that posts to an external platform is unambiguous — that *is* the
    # application flow, so it counts regardless of any link text.
    for form in page.css("form"):
        action = form.attrib.get("action", "")
        if action.startswith("http") and _registrable_domain(action) != site_domain:
            if any(p in action for p in EXTERNAL_PLATFORMS):
                return action
    # A bare link to a platform only counts when the link itself shows apply
    # intent; otherwise it's most likely cross-promotion (e.g. a Pararius footer
    # link to huurwoningen.nl) rather than the route for this listing.
    for anchor in page.css("a[href]"):
        href = anchor.attrib.get("href", "")
        if not href.startswith("http"):
            continue
        if not any(p in href for p in EXTERNAL_PLATFORMS):
            continue
        if _registrable_domain(href) == site_domain:
            continue
        text = anchor.get_all_text(strip=True) if hasattr(anchor, "get_all_text") else ""
        if APPLY_INTENT_RE.search(text) or APPLY_INTENT_RE.search(href):
            return href
    return ""


def _find_contact_link(page, listing_url: str) -> str:
    site_domain = _registrable_domain(listing_url)
    for anchor in page.css("a[href]"):
        href = anchor.attrib.get("href", "")
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        text = anchor.get_all_text(strip=True) if hasattr(anchor, "get_all_text") else ""
        if not (CONTACT_LINK_RE.search(href) or CONTACT_LINK_RE.search(text or "")):
            continue
        absolute = urljoin(listing_url, href)
        if _registrable_domain(absolute) == site_domain:
            return absolute
    return ""


def detect(listing_url: str) -> ContactInfo:
    page = _fetch(listing_url)
    if page is None:
        return ContactInfo(method="unknown")

    email = _find_email(page, listing_url)

    if _has_fillable_form(page):
        return ContactInfo(
            method="form",
            detail=listing_url,
            email=email,
            captcha=_has_captcha(page),
        )

    if email:
        return ContactInfo(method="email", detail=email)

    contact_url = _find_contact_link(page, listing_url)
    if contact_url:
        contact_page = _fetch(contact_url)
        if contact_page is not None:
            contact_email = _find_email(contact_page, contact_url)
            if _has_fillable_form(contact_page):
                return ContactInfo(
                    method="form",
                    detail=contact_url,
                    email=contact_email,
                    captcha=_has_captcha(contact_page),
                )
            if contact_email:
                return ContactInfo(method="email", detail=contact_email)

    external = _find_external(page, listing_url)
    if external:
        return ContactInfo(method="external", detail=external)

    return ContactInfo(method="unknown")
