"""Shared Telegram plumbing for the responder and the sales-sidecar.

Two things live here:

* pure, backend-free helpers (``escape_html`` and the status-button keyboard)
  that both services embed verbatim in their notifications, and
* :class:`TelegramClient` — a thin ``requests``-based Bot API client used by the
  responder. ``requests`` is imported lazily inside ``_call`` so that importing
  this module (for the pure helpers) never requires ``requests`` to be present
  — the sales-sidecar imports the helpers but keeps its own ``urllib`` sender.

The status-button keyboard is deliberately identical JSON in both services: the
responder is the bot's only ``getUpdates`` consumer and dispatches the callbacks
statelessly (chat_id + message_id come from the callback query), so the same row
works on koop messages the sales-sidecar sent.
"""

import json
import logging


def escape_html(text: str) -> str:
    """Escape the characters that are special inside Telegram HTML messages."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Status buttons shown under every listing notification (rental + koop). The
# callback_data codes are kept tiny (well under Telegram's 64-byte limit) and
# carry no listing id — dispatch is stateless, so the same row works on messages
# the responder never sent (koop messages sent by the sales-sidecar).
STATUS_BUTTONS: tuple[tuple[str, str], ...] = (
    ("✅", "st:r"),  # gereageerd
    ("📅", "st:i"),  # uitgenodigd
    ("❌", "st:x"),  # afgewezen
    ("🗑", "st:d"),  # niet interessant (delete)
)


def status_button_row() -> list[dict]:
    """One row of status buttons (fresh list each call — never mutated)."""
    return [{"text": emoji, "callback_data": data} for emoji, data in STATUS_BUTTONS]


def status_keyboard() -> dict:
    """A single-row inline keyboard of the status buttons."""
    return {"inline_keyboard": [status_button_row()]}


class TelegramClient:
    """Minimal Telegram Bot API client (plain HTTP via ``requests``)."""

    def __init__(self, token: str, *, log: logging.Logger | None = None) -> None:
        self._token = token
        self._api = f"https://api.telegram.org/bot{token}"
        self._log = log or logging.getLogger("shared.tg")

    def _call(self, method: str, params: dict, *, files=None, timeout: int = 15):
        if not self._token:
            self._log.warning("Telegram not configured, dropping %s", method)
            return None
        import requests

        try:
            if files:
                # Multipart request: complex params must be JSON-encoded strings.
                data = {
                    k: json.dumps(v) if isinstance(v, (dict, list)) else v
                    for k, v in params.items()
                }
                resp = requests.post(
                    f"{self._api}/{method}", data=data, files=files, timeout=timeout
                )
            else:
                resp = requests.post(
                    f"{self._api}/{method}", json=params, timeout=timeout
                )
            payload = resp.json()
        except Exception as exc:
            self._log.error("Telegram %s failed: %s", method, exc)
            return None
        if not payload.get("ok"):
            self._log.error("Telegram %s rejected: %s", method, payload.get("description"))
            return None
        return payload["result"]

    def send_message(
        self,
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
        result = self._call("sendMessage", params)
        return result["message_id"] if result else None

    def send_photo(
        self,
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
                result = self._call("sendPhoto", params, files={"photo": fh}, timeout=60)
        except OSError as exc:
            self._log.error("Cannot read screenshot %s: %s", photo_path, exc)
            return None
        return result["message_id"] if result else None

    def edit_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
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
        self._call("editMessageText", params)

    def delete_message(self, chat_id: str, message_id: int) -> bool:
        """Delete a previously sent message. Telegram only allows this within
        48h; after that the API rejects it — treat that (and any error) as a soft
        failure so the caller can still mark the listing as gone."""
        result = self._call(
            "deleteMessage", {"chat_id": chat_id, "message_id": message_id}
        )
        return bool(result)

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        params: dict = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        self._call("answerCallbackQuery", params)

    def set_reaction(self, chat_id: str, message_id: int, emoji: str) -> bool:
        """Set the bot's single reaction on a message (Bot API 7.0+).

        Returns True on success. Fails (returns False) when reactions are
        disabled in the chat or the API is too old, so the caller can fall back
        to editing the message text.
        """
        return (
            self._call(
                "setMessageReaction",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                },
            )
            is not None
        )

    def get_updates(self, offset: int | None) -> list[dict] | None:
        """Long-poll for updates; None means the call failed (back off)."""
        params: dict = {
            "timeout": 50,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params, timeout=60)
