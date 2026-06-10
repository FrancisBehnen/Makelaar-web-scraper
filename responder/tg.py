"""Thin Telegram Bot API client (plain HTTP, no bot framework)."""

import json
import logging

import requests

from config import TELEGRAM_ALERT_CHAT_IDS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS

log = logging.getLogger("responder")

_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _call(method: str, params: dict, *, files=None, timeout: int = 15):
    if not TELEGRAM_BOT_TOKEN:
        log.warning("Telegram not configured, dropping %s", method)
        return None
    try:
        if files:
            # Multipart request: complex params must be JSON-encoded strings.
            data = {
                k: json.dumps(v) if isinstance(v, (dict, list)) else v
                for k, v in params.items()
            }
            resp = requests.post(
                f"{_API}/{method}", data=data, files=files, timeout=timeout
            )
        else:
            resp = requests.post(f"{_API}/{method}", json=params, timeout=timeout)
        payload = resp.json()
    except Exception as exc:
        log.error("Telegram %s failed: %s", method, exc)
        return None
    if not payload.get("ok"):
        log.error("Telegram %s rejected: %s", method, payload.get("description"))
        return None
    return payload["result"]


def send_message(
    chat_id: str,
    text: str,
    *,
    reply_markup: dict | None = None,
    reply_to: int | None = None,
) -> int | None:
    params: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        params["reply_markup"] = reply_markup
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = True
    result = _call("sendMessage", params)
    return result["message_id"] if result else None


def broadcast(text: str, *, reply_markup: dict | None = None) -> dict[str, int]:
    """Send to every configured chat; returns {chat_id: message_id}."""
    message_ids: dict[str, int] = {}
    for chat_id in TELEGRAM_CHAT_IDS:
        message_id = send_message(chat_id, text, reply_markup=reply_markup)
        if message_id is not None:
            message_ids[chat_id] = message_id
    return message_ids


def send_photo(
    chat_id: str,
    photo_path: str,
    caption: str,
    *,
    reply_markup: dict | None = None,
) -> int | None:
    # Captions are plain text (no parse_mode) so screenshots never fail on
    # markup in form values; max caption length is 1024.
    params: dict = {"chat_id": chat_id, "caption": caption[:1024]}
    if reply_markup:
        params["reply_markup"] = reply_markup
    try:
        with open(photo_path, "rb") as fh:
            result = _call("sendPhoto", params, files={"photo": fh}, timeout=60)
    except OSError as exc:
        log.error("Cannot read screenshot %s: %s", photo_path, exc)
        return None
    return result["message_id"] if result else None


def broadcast_photo(
    photo_path: str, caption: str, *, reply_markup: dict | None = None
) -> dict[str, int]:
    message_ids: dict[str, int] = {}
    for chat_id in TELEGRAM_CHAT_IDS:
        message_id = send_photo(
            chat_id, photo_path, caption, reply_markup=reply_markup
        )
        if message_id is not None:
            message_ids[chat_id] = message_id
    return message_ids


def edit_text(
    chat_id: str, message_id: int, text: str, *, reply_markup: dict | None = None
) -> None:
    params: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    _call("editMessageText", params)


def answer_callback(callback_id: str, text: str | None = None) -> None:
    params: dict = {"callback_query_id": callback_id}
    if text:
        params["text"] = text
    _call("answerCallbackQuery", params)


def get_updates(offset: int | None) -> list[dict] | None:
    """Long-poll for updates; None means the call failed (back off)."""
    params: dict = {
        "timeout": 50,
        "allowed_updates": ["message", "callback_query"],
    }
    if offset is not None:
        params["offset"] = offset
    return _call("getUpdates", params, timeout=60)


def send_alert(text: str) -> None:
    """Operational alert (errors etc.), separate from listing notifications."""
    for chat_id in TELEGRAM_ALERT_CHAT_IDS:
        send_message(chat_id, text)
