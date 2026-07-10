"""Environment configuration shared by all responder modules."""

import os
import threading

DB_PATH = os.environ.get("DB_PATH", "data/db.sqlite")
DATA_DIR = os.environ.get("DATA_DIR", "data")
SCREENSHOT_DIR = os.path.join(DATA_DIR, "screenshots")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Chats that receive notifications AND are the only chats allowed to trigger
# callbacks/commands. Everything from other chats is ignored.
TELEGRAM_CHAT_IDS: list[str] = [
    c.strip()
    for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
    if c.strip()
]
# For-sale ("Huisje kopen") chats. A URL sent here opens an add-site issue
# targeting the sales-sidecar instead of the rental python-sidecar. These chats
# never receive rental listing notifications or contact-form flows.
TELEGRAM_SALES_CHAT_IDS: list[str] = [
    c.strip()
    for c in os.environ.get("TELEGRAM_SALES_CHAT_IDS", "").split(",")
    if c.strip()
]
# Operational alerts; falls back to the regular chats when unset.
TELEGRAM_ALERT_CHAT_IDS: list[str] = [
    c.strip()
    for c in os.environ.get("TELEGRAM_ALERT_CHAT_IDS", "").split(",")
    if c.strip()
] or TELEGRAM_CHAT_IDS

# Personal details used to fill contact forms.
CONTACT_NAME = os.environ.get("CONTACT_NAME", "")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
CONTACT_PHONE = os.environ.get("CONTACT_PHONE", "")

GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "FrancisBehnen/Makelaar-web-scraper")

# Optional logged-in session cookie for Huurstunt (raw `Cookie:` header value,
# e.g. "a=b; c=d"). Huurstunt gates listing detail behind an email magic-link
# login, so detection of the contact route needs the same session the scraper
# used to discover the listing. Re-capture from a browser when it expires.
HUURSTUNT_COOKIE = os.environ.get("HUURSTUNT_COOKIE", "").strip()

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "120"))

# Delisting re-check: every RECHECK_INTERVAL seconds a batch of previously
# notified, still "available" listings is re-fetched over plain HTTP to spot
# ones that have been rented out / removed, so their Telegram notification can
# be deleted and a short summary sent (mirrors the sales-sidecar mechanism).
RECHECK_INTERVAL = int(os.environ.get("RENTAL_RECHECK_INTERVAL", "600"))
RECHECK_BATCH_SIZE = int(os.environ.get("RENTAL_RECHECK_BATCH_SIZE", "5"))

# Only one Camoufox instance may run at a time (VPS memory). Both contact
# detection (StealthyFetcher) and the form-fill jobs take this lock around
# any browser use.
BROWSER_LOCK = threading.Lock()
