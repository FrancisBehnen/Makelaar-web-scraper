"""Thin Telegram Bot API wrapper for the responder.

The HTTP plumbing and the status-button keyboard live in ``shared.tg`` (reused
by the sales-sidecar). This module binds a single client to the responder's bot
token and exposes the broadcast/alert helpers scoped to the configured chats.
"""

import logging

from shared.tg import (  # noqa: F401  (re-exported for callers/tests)
    STATUS_BUTTONS,
    TelegramClient,
    escape_html,
    status_button_row,
    status_keyboard,
)

from config import TELEGRAM_ALERT_CHAT_IDS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS

log = logging.getLogger("responder")

_client = TelegramClient(TELEGRAM_BOT_TOKEN, log=log)


def send_message(
    chat_id: str,
    text: str,
    *,
    reply_markup: dict | None = None,
    reply_to: int | None = None,
    disable_notification: bool = False,
) -> int | None:
    return _client.send_message(
        chat_id,
        text,
        reply_markup=reply_markup,
        reply_to=reply_to,
        disable_notification=disable_notification,
    )


def broadcast(
    text: str, *, reply_markup: dict | None = None, disable_notification: bool = False
) -> dict[str, int]:
    """Send to every configured chat; returns {chat_id: message_id}.

    ``disable_notification`` sends silently (no push) — used for the
    accumulating gone-summary so only new listings ping the group."""
    message_ids: dict[str, int] = {}
    for chat_id in TELEGRAM_CHAT_IDS:
        message_id = send_message(
            chat_id,
            text,
            reply_markup=reply_markup,
            disable_notification=disable_notification,
        )
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
    return _client.send_photo(
        chat_id, photo_path, caption, reply_markup=reply_markup
    )


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
) -> bool:
    return _client.edit_text(chat_id, message_id, text, reply_markup=reply_markup)


def delete_message(chat_id: str, message_id: int) -> bool:
    return _client.delete_message(chat_id, message_id)


def answer_callback(callback_id: str, text: str | None = None) -> None:
    _client.answer_callback(callback_id, text)


def set_reaction(chat_id: str, message_id: int, emoji: str) -> bool:
    return _client.set_reaction(chat_id, message_id, emoji)


def get_updates(offset: int | None) -> list[dict] | None:
    return _client.get_updates(offset)


def send_alert(text: str) -> None:
    """Operational alert (errors etc.), separate from listing notifications."""
    for chat_id in TELEGRAM_ALERT_CHAT_IDS:
        send_message(chat_id, text)
