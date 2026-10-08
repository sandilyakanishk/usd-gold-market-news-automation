"""Telegram Bot API client: the only place that talks to Telegram.

Delivery only. It sends the text it is given, unchanged, and never composes
or edits a message. The bot token is part of every request URL, so no URL is
ever included in an error message or a log line.
"""

from __future__ import annotations

import json
import logging
import uuid
import urllib.error
import urllib.request

from .models import DeliveryError

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.telegram.org"
USER_AGENT = "usd-gold-calendar-collector/0.1 (personal, low-frequency)"
MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
# Telegram's simple Markdown reads *bold* and _italic_ the same way WhatsApp does.
PARSE_MODE = "Markdown"


class TelegramError(DeliveryError):
    """A Telegram request failed. Messages never contain the token."""


class TelegramConfigError(TelegramError):
    """A required setting is missing."""


class TelegramAuthError(TelegramError):
    """The bot token was rejected."""


class TelegramDestinationError(TelegramError):
    """The chat does not exist, or the bot may not post there."""


class TelegramNetworkError(TelegramError):
    """Timeout or connection failure."""


class TelegramMessageError(TelegramError):
    """The message itself cannot be sent (empty, too long, not text)."""


def describe_chat(chat_id: str | None) -> str:
    """A chat reference that is safe to print: public @names as they are, numeric ids masked."""
    if not chat_id:
        return "(not set)"
    chat_id = str(chat_id)
    return chat_id if chat_id.startswith("@") else f"...{chat_id[-4:]}"


class TelegramClient:
    def __init__(self, token: str | None, *, base_url: str = DEFAULT_BASE_URL, timeout: int = 20):
        self._token = (token or "").strip()
        if not self._token:
            raise TelegramConfigError("TELEGRAM_BOT_TOKEN is not set. Add it to the environment or to .env.")
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _clean(self, text: str) -> str:
        return text.replace(self._token, "***")

    def _call(self, method: str, payload: dict | None = None) -> dict | list | bool | int:
        """Call one Bot API method and return its `result`."""
        return self._request(method, json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"), "application/json")

    def _request(self, method: str, data: bytes, content_type: str) -> dict | list | bool | int:
        request = urllib.request.Request(
            f"{self._base}/bot{self._token}/{method}", data=data, method="POST",
            headers={"Content-Type": content_type, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise self._api_error(exc.code, self._description(exc)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TelegramNetworkError(self._clean(f"Could not reach Telegram: {reason}")) from None
        try:
            parsed = json.loads(body)
        except ValueError:
            raise TelegramError("Telegram returned a response that is not JSON.") from None
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            description = parsed.get("description", "") if isinstance(parsed, dict) else ""
            raise self._api_error(parsed.get("error_code", 0) if isinstance(parsed, dict) else 0, str(description))
        return parsed.get("result")

    @staticmethod
    def _description(exc: urllib.error.HTTPError) -> str:
        try:
            parsed = json.loads(exc.read().decode("utf-8", errors="replace"))
            return str(parsed.get("description", "")) if isinstance(parsed, dict) else ""
        except (ValueError, OSError, AttributeError):
            return ""

    def _api_error(self, code: int, description: str) -> TelegramError:
        text = self._clean(f"Telegram answered {code}: {description}".strip().rstrip(":"))
        lowered = description.lower()
        if code in (401, 404) and ("unauthorized" in lowered or "not found" == lowered.strip() or not lowered):
            return TelegramAuthError(f"{text}. Check TELEGRAM_BOT_TOKEN.")
        if "chat not found" in lowered or "not a member" in lowered or "kicked" in lowered or "rights" in lowered \
                or "not enough" in lowered or code == 403:
            return TelegramDestinationError(
                f"{text}. Check TELEGRAM_CHAT_ID and that the bot is an administrator of the channel with permission to post.")
        if "can't parse entities" in lowered or "message is too long" in lowered or "message text is empty" in lowered:
            return TelegramMessageError(text)
        return TelegramError(text)

    # -- operations ---------------------------------------------------------------------

    def bot_username(self) -> str:
        """The bot's @username, which also proves the token is valid."""
        result = self._call("getMe")
        return str(result.get("username", "")) if isinstance(result, dict) else ""

    def check_destination(self, chat_id: str) -> dict:
        """Confirm the chat exists and that the bot may post there. Sends nothing."""
        if not chat_id or not str(chat_id).strip():
            raise TelegramConfigError("TELEGRAM_CHAT_ID is not set. Add it to the environment or to .env.")
        me = self._call("getMe")
        chat = self._call("getChat", {"chat_id": chat_id})
        member = self._call("getChatMember", {"chat_id": chat_id, "user_id": me["id"]})
        status = member.get("status")
        may_post = status == "creator" or (status == "administrator" and member.get("can_post_messages", True))
        if chat.get("type") != "channel":
            may_post = status in ("creator", "administrator", "member")
        if not may_post:
            raise TelegramDestinationError(
                f"The bot is '{status}' in {describe_chat(chat_id)} and may not post there. "
                "Make it an administrator with permission to post messages.")
        return {"bot": me.get("username"), "title": chat.get("title"), "type": chat.get("type"), "status": status}

    def send_text(self, chat_id: str, text: str, *, markdown: bool = True, link_preview: bool = False) -> str:
        """Send `text` exactly as given. Returns Telegram's message id.

        `markdown` only tells Telegram whether to render *bold* and _italic_;
        the text itself is identical either way. If Telegram cannot read the
        markers, the same text is sent once more as plain text.
        """
        if not chat_id or not str(chat_id).strip():
            raise TelegramConfigError("TELEGRAM_CHAT_ID is not set. Add it to the environment or to .env.")
        if not isinstance(text, str) or not text.strip():
            raise TelegramMessageError("The message has no text.")
        if len(text) > MAX_TEXT_LENGTH:
            raise TelegramMessageError(f"The message is {len(text)} characters long; the limit is {MAX_TEXT_LENGTH}.")
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": not link_preview}
        try:
            result = self._call("sendMessage", {**payload, "parse_mode": PARSE_MODE} if markdown else payload)
        except TelegramMessageError as exc:
            if not markdown or "parse entities" not in str(exc).lower():
                raise
            # Nothing was posted: Telegram rejected the formatting. The same text goes out unformatted.
            log.warning("Telegram could not read the formatting of a message; sending it as plain text.")
            result = self._call("sendMessage", payload)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            raise TelegramError("Telegram did not confirm the message: no message id was returned.")
        log.info("Telegram: message accepted for %s (id %s)", describe_chat(chat_id), message_id)
        return str(message_id)

    def send_photo(self, chat_id: str, photo_url: str, caption: str) -> str:
        """Post the image at `photo_url` with `caption` under it, as plain text. Returns Telegram's message id.

        Telegram fetches the image itself; nothing is downloaded here.
        """
        if not chat_id or not str(chat_id).strip():
            raise TelegramConfigError("TELEGRAM_CHAT_ID is not set. Add it to the environment or to .env.")
        if not isinstance(photo_url, str) or not photo_url.startswith("https://"):
            raise TelegramMessageError("The image address must start with https://.")
        if not isinstance(caption, str) or not caption.strip():
            raise TelegramMessageError("The message has no text.")
        if len(caption) > MAX_CAPTION_LENGTH:
            raise TelegramMessageError(f"The caption is {len(caption)} characters long; the limit is {MAX_CAPTION_LENGTH}.")
        result = self._call("sendPhoto", {"chat_id": chat_id, "photo": photo_url, "caption": caption})
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            raise TelegramError("Telegram did not confirm the message: no message id was returned.")
        log.info("Telegram: photo accepted for %s (id %s)", describe_chat(chat_id), message_id)
        return str(message_id)

    def send_photo_bytes(self, chat_id: str, image: bytes, caption: str, *, filename: str = "cover.jpg") -> str:
        """Upload `image` and post it with `caption` under it, as plain text. Returns Telegram's message id."""
        if not chat_id or not str(chat_id).strip():
            raise TelegramConfigError("TELEGRAM_CHAT_ID is not set. Add it to the environment or to .env.")
        if not isinstance(image, (bytes, bytearray)) or not image:
            raise TelegramMessageError("There is no image to send.")
        if len(image) > MAX_PHOTO_BYTES:
            raise TelegramMessageError(f"The image is {len(image)} bytes; the limit is {MAX_PHOTO_BYTES}.")
        if not isinstance(caption, str) or not caption.strip():
            raise TelegramMessageError("The message has no text.")
        if len(caption) > MAX_CAPTION_LENGTH:
            raise TelegramMessageError(f"The caption is {len(caption)} characters long; the limit is {MAX_CAPTION_LENGTH}.")
        boundary = f"----pulse{uuid.uuid4().hex}"
        parts = []
        for name, value in (("chat_id", str(chat_id)), ("caption", caption)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
                      "Content-Type: image/jpeg\r\n\r\n").encode("utf-8") + bytes(image) + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        result = self._request("sendPhoto", b"".join(parts), f"multipart/form-data; boundary={boundary}")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            raise TelegramError("Telegram did not confirm the message: no message id was returned.")
        log.info("Telegram: photo accepted for %s (id %s)", describe_chat(chat_id), message_id)
        return str(message_id)

    def send_quiz(self, chat_id: str, question: str, options: list[str], correct: int, explanation: str = "") -> str:
        """Post a quiz poll: readers tap an answer and see whether it was right. Returns Telegram's message id."""
        if not chat_id or not str(chat_id).strip():
            raise TelegramConfigError("TELEGRAM_CHAT_ID is not set. Add it to the environment or to .env.")
        if not isinstance(question, str) or not (1 <= len(question.strip()) <= 300):
            raise TelegramMessageError("A quiz question must have 1 to 300 characters.")
        if not isinstance(options, list) or not (2 <= len(options) <= 10) \
                or any(not isinstance(o, str) or not (1 <= len(o.strip()) <= 100) for o in options):
            raise TelegramMessageError("A quiz needs 2 to 10 options of 1 to 100 characters each.")
        if isinstance(correct, bool) or not isinstance(correct, int) or not (0 <= correct < len(options)):
            raise TelegramMessageError("The correct option must be one of the options.")
        if len(explanation) > 200:
            raise TelegramMessageError("A quiz explanation may have at most 200 characters.")
        payload = {"chat_id": chat_id, "question": question, "options": [{"text": o} for o in options],
                   "type": "quiz", "correct_option_id": correct, "is_anonymous": True}
        if explanation.strip():
            payload["explanation"] = explanation
        result = self._call("sendPoll", payload)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            raise TelegramError("Telegram did not confirm the message: no message id was returned.")
        log.info("Telegram: quiz accepted for %s (id %s)", describe_chat(chat_id), message_id)
        return str(message_id)
