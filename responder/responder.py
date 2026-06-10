"""Listing responder service.

Watches the shared SQLite DB for listings the scrapers found, detects how the
makelaar wants to be contacted and drives the respond flow from Telegram:

- one clean notification per listing, letter behind a button,
- e-mail contacts: copyable address/subject/letter,
- contact forms: filled in a headless browser, screenshot + ✅/❌ approval
  before anything is submitted,
- a listing URL of an untracked site sent to the bot opens a GitHub issue
  that the claude-add-site workflow turns into a PR.
"""

import json
import logging
import os
import queue
import re
import threading
import time

import config
import db
import detection
import form_filler
import github_issues
import letter
import tg
from letter import escape_html as esc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("responder")

URL_RE = re.compile(r'https?://[^\s<>"]+')

STATUS_EMOJI = {
    "detecting": "🔍",
    "notified": "🆕",
    "filling": "⏳",
    "awaiting_approval": "👀",
    "submitting": "📤",
    "sent": "✅",
    "manual": "✍️",
    "cancelled": "❌",
    "failed": "⚠️",
}

# Browser work (form fill/submit) runs on a single worker so only one
# Camoufox instance exists at a time.
browser_jobs: queue.Queue[tuple[str, int]] = queue.Queue()


# ---------------------------------------------------------------------------
# Notification rendering
# ---------------------------------------------------------------------------


def _contact_line(method: str, detail: str, email: str) -> str:
    if method == "email":
        return f"✉️ E-mail: <code>{esc(detail)}</code>"
    if method == "form":
        line = "📝 Contactformulier gevonden"
        if email:
            line += f"\n✉️ E-mail: <code>{esc(email)}</code>"
        return line
    if method == "external":
        return f"🌐 Reageren via: {esc(detail)}"
    return "❓ Geen contactmethode gevonden — check de listing"


def _notification_text(house, method: str, detail: str, email: str) -> str:
    return (
        "\U0001f6a8 <b>Nieuw huis gevonden!</b> \U0001f6a8\n\n"
        "<blockquote>Gegevens van het huis:\n"
        f"Adres: {esc(house['straatnaamHuisnummer'])}, {esc(house['plaats'])}\n"
        f"Plaats: {esc(house['plaats'])}\n"
        f"Vraagprijs: {esc(house['vraagprijs'])}\n"
        f"Oppervlakte: {esc(house['oppervlakte'])}\n"
        f"Kamers: {esc(house['kamers'])}\n"
        f"URL: {esc(house['url'])}"
        "</blockquote>\n\n" + _contact_line(method, detail or "", email or "")
    )


def _notification_keyboard(
    response_id: int, method: str, *, include_fill: bool = True
) -> dict:
    row = [{"text": "📋 Brief", "callback_data": f"brief:{response_id}"}]
    if method == "form" and include_fill:
        row.append(
            {"text": "✍️ Vul formulier in", "callback_data": f"fill:{response_id}"}
        )
    return {"inline_keyboard": [row]}


def _refresh_notification(
    response_id: int, *, suffix: str, include_fill: bool
) -> None:
    row = db.get_response(response_id)
    if row is None:
        return
    house = db.get_house(row["url"])
    if house is None:
        return
    text = _notification_text(
        house, row["contact_method"], row["contact_detail"], row["contact_email"]
    )
    if suffix:
        text += f"\n\n{suffix}"
    keyboard = _notification_keyboard(
        response_id, row["contact_method"], include_fill=include_fill
    )
    for chat_id, message_id in json.loads(row["tg_message_ids"] or "{}").items():
        tg.edit_text(chat_id, message_id, text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# New-listing watcher
# ---------------------------------------------------------------------------


def _process_new_listing(url: str) -> None:
    house = db.get_house(url)
    if house is None:
        return
    response_id = db.create_response(url, "detecting")
    try:
        info = detection.detect(url)
    except Exception:
        log.exception("Contact detection failed for %s", url)
        info = detection.ContactInfo(method="unknown")

    text = _notification_text(house, info.method, info.detail, info.email)
    keyboard = _notification_keyboard(response_id, info.method)
    message_ids = tg.broadcast(text, reply_markup=keyboard)
    db.update_response(
        response_id,
        status="notified",
        contact_method=info.method,
        contact_detail=info.detail,
        contact_email=info.email,
        tg_message_ids=json.dumps(message_ids),
    )
    log.info(
        "Notified %s (%s, contact: %s %s)",
        house["straatnaamHuisnummer"],
        url,
        info.method,
        info.detail,
    )


def watcher_loop() -> None:
    if db.responses_count() == 0:
        seeded = db.seed_existing()
        if seeded:
            log.info("First run: seeded %d existing listings (not notified)", seeded)
    while True:
        try:
            for url in db.new_house_urls():
                _process_new_listing(url)
        except Exception:
            log.exception("Watcher cycle failed")
        time.sleep(config.POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Telegram update handling
# ---------------------------------------------------------------------------


def _send_help(chat_id: str) -> None:
    tg.send_message(
        chat_id,
        "🤖 <b>Listing responder</b>\n\n"
        "• Nieuwe listings verschijnen hier automatisch met een 📋 Brief-knop.\n"
        "• Bij een contactformulier: ✍️ vult het in, jij keurt de screenshot "
        "goed met ✅ voordat er iets wordt verstuurd.\n"
        "• Stuur een listing-URL van een nieuwe makelaarssite om die aan de "
        "scraper te laten toevoegen.\n"
        "• /status — laatste reacties en hun status.",
    )


def _send_status(chat_id: str) -> None:
    rows = db.recent_responses()
    if not rows:
        tg.send_message(chat_id, "Nog geen reacties geregistreerd.")
        return
    lines = []
    for row in rows:
        adres = row["straatnaamHuisnummer"] or row["url"]
        plaats = f", {esc(row['plaats'])}" if row["plaats"] else ""
        emoji = STATUS_EMOJI.get(row["status"], "•")
        lines.append(
            f"{emoji} {esc(adres)}{plaats} — "
            f"{row['contact_method'] or '?'} ({row['status']})"
        )
    tg.send_message(chat_id, "📊 <b>Laatste reacties</b>\n\n" + "\n".join(lines))


def _propose_add_site(chat_id: str, url: str) -> None:
    token = str(int(time.time() * 1000))
    db.kv_set(f"addsite:{token}", url)
    domain = github_issues.domain_of(url)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Maak issue", "callback_data": f"siteok:{token}"},
                {"text": "❌ Annuleer", "callback_data": f"siteno:{token}"},
            ]
        ]
    }
    tg.send_message(
        chat_id,
        f"🆕 <b>Site toevoegen aan de scraper?</b>\n\n"
        f"<code>{esc(domain)}</code>\n{esc(url)}\n\n"
        "Er wordt een GitHub-issue aangemaakt; Claude Code maakt daar "
        "vervolgens een PR van.",
        reply_markup=keyboard,
    )


def _handle_message(message: dict) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id not in config.TELEGRAM_CHAT_IDS:
        return
    text = (message.get("text") or "").strip()
    if not text:
        return
    if text.startswith(("/start", "/help")):
        _send_help(chat_id)
        return
    if text.startswith("/status"):
        _send_status(chat_id)
        return
    match = URL_RE.search(text)
    if match:
        _propose_add_site(chat_id, match.group(0).rstrip(".,)"))


def _handle_brief(chat_id: str, message_id: int, row) -> None:
    house = db.get_house(row["url"])
    if house is None:
        return
    adres = house["straatnaamHuisnummer"]
    brief = letter.aanmeldbrief(adres)
    if row["contact_method"] == "email":
        text = (
            f"✉️ <b>Reactie voor {esc(adres)}</b>\n\n"
            f"Aan: <code>{esc(row['contact_detail'])}</code>\n"
            f"Onderwerp: <code>{esc(letter.subject(adres, house['plaats']))}</code>\n\n"
            f"<pre>{esc(brief)}</pre>"
        )
    else:
        text = f"📋 <b>Brief voor {esc(adres)}</b>\n\n<pre>{esc(brief)}</pre>"
    tg.send_message(chat_id, text, reply_to=message_id)


def _handle_callback(callback: dict) -> None:
    callback_id = callback["id"]
    message = callback.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id not in config.TELEGRAM_CHAT_IDS:
        tg.answer_callback(callback_id)
        return
    action, _, arg = (callback.get("data") or "").partition(":")

    if action == "siteok":
        url = db.kv_get(f"addsite:{arg}")
        if not url:
            tg.answer_callback(callback_id, "Verzoek is verlopen, stuur de URL opnieuw")
            return
        db.kv_delete(f"addsite:{arg}")
        tg.answer_callback(callback_id, "Issue wordt aangemaakt…")
        try:
            issue_url = github_issues.create_add_site_issue(url)
            tg.send_message(chat_id, f"✅ Issue aangemaakt: {esc(issue_url)}")
        except Exception as exc:
            log.exception("Add-site issue creation failed")
            tg.send_message(chat_id, f"⚠️ Issue aanmaken mislukt: {esc(str(exc))}")
        return
    if action == "siteno":
        db.kv_delete(f"addsite:{arg}")
        tg.answer_callback(callback_id, "Oké, niets gedaan")
        return

    try:
        response_id = int(arg)
    except ValueError:
        tg.answer_callback(callback_id)
        return
    row = db.get_response(response_id)
    if row is None:
        tg.answer_callback(callback_id, "Onbekende reactie")
        return

    if action == "brief":
        _handle_brief(chat_id, message.get("message_id"), row)
        tg.answer_callback(callback_id)
    elif action == "fill":
        if row["contact_method"] != "form":
            tg.answer_callback(callback_id, "Geen formulier gedetecteerd")
        elif row["status"] in ("filling", "submitting"):
            tg.answer_callback(callback_id, "Al bezig…")
        elif row["status"] == "sent":
            tg.answer_callback(callback_id, "Al verstuurd ✅")
        else:
            db.update_response(response_id, status="filling")
            browser_jobs.put(("prepare", response_id))
            tg.answer_callback(
                callback_id, "Formulier wordt ingevuld, screenshot volgt…"
            )
    elif action == "ok":
        if row["status"] != "awaiting_approval":
            tg.answer_callback(callback_id, "Niets om te versturen")
        else:
            db.update_response(response_id, status="submitting")
            browser_jobs.put(("submit", response_id))
            tg.answer_callback(callback_id, "Wordt verstuurd…")
    elif action == "no":
        if row["status"] != "awaiting_approval":
            tg.answer_callback(callback_id, "Niets om te annuleren")
        else:
            db.update_response(response_id, status="cancelled")
            _refresh_notification(
                response_id, suffix="❌ Reactie geannuleerd", include_fill=True
            )
            tg.answer_callback(callback_id, "Geannuleerd")
    else:
        tg.answer_callback(callback_id)


def _handle_update(update: dict) -> None:
    if "callback_query" in update:
        _handle_callback(update["callback_query"])
    elif "message" in update:
        _handle_message(update["message"])


def bot_loop() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; bot loop idle")
        while True:
            time.sleep(3600)
    raw_offset = db.kv_get("tg_offset")
    offset = int(raw_offset) if raw_offset else None
    while True:
        updates = tg.get_updates(offset)
        if updates is None:
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                _handle_update(update)
            except Exception:
                log.exception("Failed to handle update %s", update.get("update_id"))
            db.kv_set("tg_offset", str(offset))


# ---------------------------------------------------------------------------
# Browser worker (form fill / submit)
# ---------------------------------------------------------------------------


def _run_browser_job(kind: str, response_id: int) -> None:
    row = db.get_response(response_id)
    house = db.get_house(row["url"]) if row else None
    if row is None or house is None:
        return
    adres = house["straatnaamHuisnummer"]
    form_url = row["contact_detail"] or row["url"]

    if kind == "prepare":
        screenshot = os.path.join(config.SCREENSHOT_DIR, f"{response_id}_form.png")
        try:
            plan = form_filler.prepare(form_url, house, screenshot)
        except form_filler.FormFillError as exc:
            db.update_response(response_id, status="manual", error=str(exc))
            tg.broadcast(
                f"✍️ Formulier voor <b>{esc(adres)}</b> kon niet automatisch "
                f"worden ingevuld: {esc(str(exc))}.\n"
                f"Handmatig reageren: {esc(form_url)}",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "📋 Brief", "callback_data": f"brief:{response_id}"}]
                    ]
                },
            )
            return
        db.update_response(
            response_id,
            status="awaiting_approval",
            form_data=json.dumps(plan),
            screenshot_path=screenshot,
        )
        caption = (
            f"✍️ Formulier ingevuld voor {adres}\n\n"
            + "\n".join(plan["summary"])
            + "\n\nVersturen?"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Verstuur", "callback_data": f"ok:{response_id}"},
                    {"text": "❌ Annuleer", "callback_data": f"no:{response_id}"},
                ]
            ]
        }
        tg.broadcast_photo(screenshot, caption, reply_markup=keyboard)

    elif kind == "submit":
        screenshot = os.path.join(config.SCREENSHOT_DIR, f"{response_id}_result.png")
        plan = json.loads(row["form_data"])
        try:
            form_filler.submit(plan, screenshot)
        except form_filler.FormFillError as exc:
            db.update_response(response_id, status="failed", error=str(exc))
            tg.broadcast(
                f"⚠️ Versturen mislukt voor <b>{esc(adres)}</b>: {esc(str(exc))}\n"
                f"Handmatig reageren: {esc(form_url)}"
            )
            return
        now = time.strftime("%Y-%m-%d %H:%M")
        db.update_response(response_id, status="sent", screenshot_path=screenshot)
        tg.broadcast_photo(screenshot, f"✅ Reactie verstuurd voor {adres} ({now})")
        _refresh_notification(
            response_id, suffix=f"✅ Verzonden op {now}", include_fill=False
        )
        log.info("Submitted contact form for %s (%s)", adres, row["url"])


def worker_loop() -> None:
    while True:
        kind, response_id = browser_jobs.get()
        try:
            _run_browser_job(kind, response_id)
        except Exception as exc:
            log.exception("Browser job %s for response %d failed", kind, response_id)
            db.update_response(response_id, status="failed", error=str(exc))
            tg.send_alert(
                f"⚠️ Responder: browseractie '{kind}' mislukt voor reactie "
                f"{response_id}: {exc}"
            )
        finally:
            browser_jobs.task_done()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    os.makedirs(config.SCREENSHOT_DIR, exist_ok=True)
    db.init_schema()
    log.info(
        "Responder starting (%d notification chat(s) configured)",
        len(config.TELEGRAM_CHAT_IDS),
    )
    threads = [
        threading.Thread(target=watcher_loop, name="watcher", daemon=True),
        threading.Thread(target=bot_loop, name="bot", daemon=True),
        threading.Thread(target=worker_loop, name="browser-worker", daemon=True),
    ]
    for thread in threads:
        thread.start()
    while True:
        for thread in threads:
            if not thread.is_alive():
                log.error("Thread %s died; exiting so Docker restarts us", thread.name)
                tg.send_alert(
                    f"⚠️ Responder-thread '{thread.name}' is gestopt; "
                    "container wordt herstart"
                )
                os._exit(1)
        time.sleep(5)


if __name__ == "__main__":
    main()
