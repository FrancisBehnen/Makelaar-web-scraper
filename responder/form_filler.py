"""Two-phase contact-form automation in a headless Camoufox browser.

Phase 1 (:func:`prepare`) opens the form page, maps fields by Dutch/English
keyword heuristics, fills them, screenshots the result and returns a *plan*:
the exact field selectors and values. Nothing is submitted.

Phase 2 (:func:`submit`) — only ever run after a ✅ in Telegram — reopens the
page, re-applies the same plan deterministically and clicks the submit button.

A plan survives the wait for approval in the DB (``responses.form_data``), so
no browser stays open in between.
"""

import logging
import re
from contextlib import contextmanager

from camoufox.sync_api import Camoufox

from config import (
    BROWSER_LOCK,
    CONTACT_EMAIL,
    CONTACT_NAME,
    CONTACT_PHONE,
    FETCH_TIMEOUT,
)
from detection import CAPTCHA_SELECTOR, NON_CONTACT_FORM_RE
import letter

log = logging.getLogger("responder")


class FormFillError(Exception):
    """Heuristics could not handle this form; fall back to manual."""


# Checked in order; the first match wins, so the specific roles must come
# before the generic 'name' ('voornaam' would otherwise match 'naam').
ROLE_KEYWORDS = [
    ("email", ("email", "e-mail", "mail")),
    ("phone", ("telefoon", "phone", "mobiel", "mobile")),
    ("first_name", ("voornaam", "firstname", "first name", "first_name")),
    ("last_name", ("achternaam", "lastname", "last name", "last_name", "surname")),
    ("subject", ("onderwerp", "subject")),
    (
        "message",
        (
            "bericht",
            "message",
            "opmerking",
            "vraag",
            "reactie",
            "motivatie",
            "comment",
            "toelichting",
        ),
    ),
    ("name", ("naam", "name")),
]

PRIVACY_RE = re.compile(
    r"privacy|voorwaarden|akkoord|agree|terms|toestemming", re.IGNORECASE
)
NEWSLETTER_RE = re.compile(r"nieuwsbrief|newsletter|aanbod|updates", re.IGNORECASE)
SUBMIT_TEXT_RE = re.compile(r"verstuur|verzend|versturen|submit|send", re.IGNORECASE)
SELECT_PLACEHOLDER_RE = re.compile(r"kies|selecteer|maak een keuze|select", re.IGNORECASE)

_COLLECT_FORMS_JS = """
() => Array.from(document.querySelectorAll('form')).map((form, formIndex) => ({
  formIndex,
  action: form.getAttribute('action') || '',
  identity: [form.id || '', form.className || '', form.getAttribute('name') || '']
    .join(' '),
  fields: Array.from(form.elements || [])
    .filter((el) => ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))
    .map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      id: el.id || '',
      name: el.getAttribute('name') || '',
      placeholder: el.getAttribute('placeholder') || '',
      aria: el.getAttribute('aria-label') || '',
      required: !!el.required,
      visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
      label: (el.labels && el.labels.length ? el.labels[0].innerText : '').trim(),
      options:
        el.tagName === 'SELECT'
          ? Array.from(el.options).map((o) => ({
              value: o.value,
              text: (o.innerText || '').trim(),
            }))
          : null,
    })),
}))
"""


@contextmanager
def _page_on(url: str):
    with BROWSER_LOCK:
        with Camoufox(headless=True) as browser:
            page = browser.new_page()
            page.set_default_timeout(15000)
            page.goto(url, wait_until="domcontentloaded", timeout=FETCH_TIMEOUT * 1000)
            # Give client-side rendering / Cloudflare a moment to settle.
            page.wait_for_timeout(4000)
            yield page


def _haystack(field: dict) -> str:
    return " ".join(
        (
            field["name"],
            field["id"],
            field["placeholder"],
            field["aria"],
            field["label"],
        )
    ).lower()


def _role_for(field: dict) -> str | None:
    if field["tag"] == "select":
        return None
    if field["type"] == "email":
        return "email"
    if field["type"] == "tel":
        return "phone"
    hay = _haystack(field)
    for role, keywords in ROLE_KEYWORDS:
        if any(keyword in hay for keyword in keywords):
            return role
    if field["tag"] == "textarea":
        return "message"
    return None


def _selector_for(field: dict) -> str | None:
    if field["id"]:
        return f'[id="{field["id"]}"]'
    if field["name"]:
        return f'[name="{field["name"]}"]'
    if field["tag"] == "textarea":
        return "textarea"
    return None


def _display_label(field: dict) -> str:
    return (
        field["label"]
        or field["placeholder"]
        or field["aria"]
        or field["name"]
        or field["id"]
        or field["tag"]
    )


def _role_values(house) -> dict[str, str]:
    full_name = CONTACT_NAME.strip()
    parts = full_name.split()
    adres = house["straatnaamHuisnummer"]
    return {
        "name": full_name,
        "first_name": parts[0] if parts else full_name,
        "last_name": " ".join(parts[1:]) if len(parts) > 1 else full_name,
        "email": CONTACT_EMAIL,
        "phone": CONTACT_PHONE,
        "subject": letter.subject(adres, house["plaats"]),
        "message": letter.aanmeldbrief(adres),
    }


def _select_option_value(field: dict) -> str | None:
    for option in field["options"] or []:
        if option["value"] and not SELECT_PLACEHOLDER_RE.search(option["text"]):
            return option["value"]
    return None


def _plan_form(meta: dict, values: dict[str, str]) -> dict | None:
    """Map one form's fields to a fill plan, or None if it isn't usable."""
    roles_seen: set[str] = set()
    fields: list[dict] = []
    summary: list[str] = []

    for field in meta["fields"]:
        if not field["visible"]:
            continue
        selector = _selector_for(field)
        if selector is None:
            continue
        label = _display_label(field)

        if field["tag"] == "input" and field["type"] in (
            "hidden",
            "submit",
            "button",
            "image",
            "file",
            "radio",
        ):
            continue

        if field["type"] == "checkbox":
            hay = _haystack(field)
            if NEWSLETTER_RE.search(hay):
                continue
            if field["required"] or PRIVACY_RE.search(hay):
                fields.append(
                    {"selector": selector, "action": "check", "value": "", "label": label}
                )
                summary.append(f"☑️ {label}")
            continue

        if field["tag"] == "select":
            if field["required"]:
                value = _select_option_value(field)
                if value is not None:
                    fields.append(
                        {
                            "selector": selector,
                            "action": "select",
                            "value": value,
                            "label": label,
                        }
                    )
                    summary.append(f"{label}: {value}")
            continue

        role = _role_for(field)
        if role is None or role in roles_seen:
            continue
        roles_seen.add(role)
        value = values[role]
        if not value:
            continue
        fields.append(
            {"selector": selector, "action": "fill", "value": value, "label": label}
        )
        shown = value if len(value) <= 60 else value[:57] + "..."
        summary.append(f"{label}: {shown}")

    if "email" not in roles_seen or "message" not in roles_seen:
        return None
    # When the form has separate first/last name fields the generic name role
    # would duplicate; values are deduplicated by roles_seen already.
    return {
        "form_index": meta["formIndex"],
        "fields": fields,
        "summary": summary,
        "roles": sorted(roles_seen),
    }


def _build_plan(forms_meta: list[dict], house, page_url: str) -> dict:
    best: dict | None = None
    values = _role_values(house)
    for meta in forms_meta:
        if NON_CONTACT_FORM_RE.search(f'{meta["identity"]} {meta["action"]}'):
            continue
        plan = _plan_form(meta, values)
        if plan and (best is None or len(plan["roles"]) > len(best["roles"])):
            best = plan
    if best is None:
        raise FormFillError(
            "geen invulbaar contactformulier gevonden (e-mail- en berichtveld vereist)"
        )
    best["page_url"] = page_url
    return best


def _apply_plan(page, plan: dict):
    forms = page.query_selector_all("form")
    if plan["form_index"] >= len(forms):
        raise FormFillError("het formulier staat niet meer op de pagina")
    form = forms[plan["form_index"]]
    for field in plan["fields"]:
        element = form.query_selector(field["selector"])
        if element is None:
            raise FormFillError(f"veld '{field['label']}' niet gevonden")
        try:
            if field["action"] == "fill":
                element.fill(field["value"])
            elif field["action"] == "check":
                if not element.is_checked():
                    element.check()
            elif field["action"] == "select":
                element.select_option(value=field["value"])
        except Exception as exc:
            raise FormFillError(
                f"veld '{field['label']}' kon niet worden ingevuld: {exc}"
            ) from exc
    return form


def prepare(form_url: str, house, screenshot_path: str) -> dict:
    """Phase 1: fill the form, screenshot it, return the plan. No submit."""
    with _page_on(form_url) as page:
        if page.query_selector(CAPTCHA_SELECTOR):
            raise FormFillError("het formulier gebruikt een captcha")
        forms_meta = page.evaluate(_COLLECT_FORMS_JS)
        plan = _build_plan(forms_meta, house, form_url)
        form = _apply_plan(page, plan)
        try:
            form.scroll_into_view_if_needed()
        except Exception:
            pass
        page.screenshot(path=screenshot_path, full_page=True)
    return plan


def submit(plan: dict, screenshot_path: str) -> None:
    """Phase 2: re-fill from the stored plan and actually submit."""
    with _page_on(plan["page_url"]) as page:
        form = _apply_plan(page, plan)
        button = form.query_selector('button[type="submit"], input[type="submit"]')
        if button is None:
            for candidate in form.query_selector_all('button, input[type="button"]'):
                text = candidate.evaluate("(el) => el.innerText || el.value || ''")
                if SUBMIT_TEXT_RE.search(text or ""):
                    button = candidate
                    break
        if button is None:
            raise FormFillError("verstuurknop niet gevonden")
        button.click()
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        page.screenshot(path=screenshot_path, full_page=True)
