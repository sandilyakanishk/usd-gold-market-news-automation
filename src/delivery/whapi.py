"""Whapi.Cloud client: the only place that talks to WhatsApp.

Delivery only. It sends the text it is given, unchanged, and never composes
or edits a message.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .models import DeliveryError

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://gate.whapi.cloud"
# Whapi's gateway rejects generic library signatures such as "Python-urllib",
# so every request identifies this project explicitly.
WHAPI_USER_AGENT = "usd-gold-calendar-collector/0.1 (personal, low-frequency)"
MAX_TEXT_LENGTH = 4096


class WhapiError(DeliveryError):
    """A Whapi request failed. Messages never contain the token."""


class WhapiConfigError(WhapiError):
    """A required setting is missing."""


class WhapiAuthError(WhapiError):
    """The token was rejected."""


class WhapiSessionError(WhapiError):
    """The WhatsApp session behind the channel is not connected."""


class WhapiDestinationError(WhapiError):
    """The destination chat does not exist or cannot be written to."""


class WhapiNetworkError(WhapiError):
    """Timeout or connection failure."""


class WhapiMessageError(WhapiError):
    """The message itself cannot be sent (empty, too long, not text)."""


def mask_chat_id(chat_id: str | None) -> str:
    """A chat id that is safe to print: the last four digits only."""
    if not chat_id:
        return "(not set)"
    local, _, domain = chat_id.partition("@")
    return f"...{local[-4:]}@{domain}" if domain else f"...{local[-4:]}"


def is_channel(chat_id: str | None) -> bool:
    """A WhatsApp Channel's id ends in @newsletter; a group's ends in @g.us."""
    return bool(chat_id) and str(chat_id).strip().endswith("@newsletter")


class WhapiClient:
    def __init__(self, token: str | None, *, base_url: str = DEFAULT_BASE_URL, timeout: int = 20,
                 user_agent: str | None = None):
        self._token = (token or "").strip()
        if not self._token:
            raise WhapiConfigError("WHAPI_TOKEN is not set. Add it to the environment or to .env.")
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        agent = (user_agent or "").strip()
        self._user_agent = agent if agent and "python-urllib" not in agent.lower() else WHAPI_USER_AGENT

    # -- transport --------------------------------------------------------------------

    def _clean(self, text: str) -> str:
        return text.replace(self._token, "***")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                   "User-Agent": self._user_agent}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise WhapiNetworkError(self._clean(f"Could not reach Whapi: {reason}")) from None
        try:
            parsed = json.loads(body) if body.strip() else {}
        except ValueError:
            raise WhapiError("Whapi returned a response that is not JSON.") from None
        if not isinstance(parsed, dict):
            raise WhapiError("Whapi returned an unexpected response.")
        return parsed

    def _http_error(self, exc: urllib.error.HTTPError) -> WhapiError:
        detail = ""
        try:
            parsed = json.loads(exc.read().decode("utf-8", errors="replace"))
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("details") or "")
            elif isinstance(parsed, dict):
                detail = str(error or parsed.get("message") or parsed.get("title") or parsed.get("detail") or "")
        except (ValueError, OSError, AttributeError):
            pass
        text = self._clean(f"Whapi answered HTTP {exc.code}. {detail}".strip())
        lowered = detail.lower()
        if exc.code == 401 or (exc.code == 403 and "signature" not in lowered):
            return WhapiAuthError(f"{text} Check WHAPI_TOKEN.")
        if exc.code in (400, 404) and any(word in lowered for word in ("chat", "recipient", "group", "not found")):
            return WhapiDestinationError(f"{text} Check WHATSAPP_ANNOUNCEMENT_CHAT_ID.")
        if exc.code in (409, 424, 503) or "session" in lowered or "not authorized" in lowered or "channel" in lowered:
            return WhapiSessionError(f"{text} Check that the WhatsApp number is still linked in the Whapi dashboard.")
        return WhapiError(text)

    # -- operations -------------------------------------------------------------------

    def health(self) -> dict:
        """The channel status. Raises WhapiSessionError unless the WhatsApp session is authenticated."""
        data = self._request("GET", "/health")
        status = data.get("status") or {}
        text = str(status.get("text", "")) if isinstance(status, dict) else str(status)
        if text.upper() != "AUTH":
            raise WhapiSessionError(
                f"The WhatsApp session is not connected (status: {text or 'unknown'}). "
                "Re-link the number in the Whapi dashboard.")
        return {"status": text}

    def announcement_group_id(self, community_id: str) -> str | None:
        """The id of a community's Announcements group, as WhatsApp reports it."""
        data = self._request("GET", f"/communities/{urllib.parse.quote(community_id, safe='@')}/subgroups")
        info = data.get("announceGroupInfo")
        return str(info.get("id")) if isinstance(info, dict) and info.get("id") else None

    def channel_role(self, channel_id: str) -> str | None:
        """This number's role in a WhatsApp Channel (owner, admin, subscriber), as WhatsApp reports it."""
        data = self._request("GET", f"/newsletters/{urllib.parse.quote(channel_id, safe='@')}")
        role = data.get("role")
        return str(role) if role else None

    def send_image(self, chat_id: str, image: bytes, caption: str) -> str:
        """Post a JPEG with `caption` under it. Returns Whapi's message id."""
        if not chat_id or not str(chat_id).strip():
            raise WhapiConfigError("WHATSAPP_ANNOUNCEMENT_CHAT_ID is not set. Add it to the environment or to .env.")
        if not isinstance(image, (bytes, bytearray)) or not image:
            raise WhapiMessageError("There is no image to send.")
        if not isinstance(caption, str) or not caption.strip():
            raise WhapiMessageError("The message has no text.")
        if len(caption) > MAX_TEXT_LENGTH:
            raise WhapiMessageError(f"The caption is {len(caption)} characters long; the limit is {MAX_TEXT_LENGTH}.")
        media = "data:image/jpeg;base64," + base64.b64encode(bytes(image)).decode("ascii")
        data = self._request("POST", "/messages/image", {"to": chat_id, "media": media, "caption": caption})
        return self._message_id(data, chat_id)

    def _message_id(self, data: dict, chat_id: str) -> str:
        message = data.get("message")
        message_id = message.get("id") if isinstance(message, dict) else None
        if data.get("sent") is False or not message_id:
            detail = data.get("error") or data.get("message") or "no message id was returned"
            raise WhapiError(self._clean(f"Whapi did not confirm the message: {detail}"))
        log.info("Whapi: message accepted for %s (id %s)", mask_chat_id(chat_id), message_id)
        return str(message_id)

    def send_text(self, chat_id: str, text: str, *, link_preview: bool = True) -> str:
        """Send `text` exactly as given. Returns Whapi's message id.

        With link_preview=False no preview card is attached to a link: Whapi's
        own server builds that card, and YouTube shows it a cookie page.
        """
        if not chat_id or not str(chat_id).strip():
            raise WhapiConfigError("WHATSAPP_ANNOUNCEMENT_CHAT_ID is not set. Add it to the environment or to .env.")
        if not isinstance(text, str) or not text.strip():
            raise WhapiMessageError("The message has no text.")
        if len(text) > MAX_TEXT_LENGTH:
            raise WhapiMessageError(f"The message is {len(text)} characters long; the limit is {MAX_TEXT_LENGTH}.")
        payload = {"to": chat_id, "body": text}
        if not link_preview:
            payload["no_link_preview"] = True
        return self._message_id(self._request("POST", "/messages/text", payload), chat_id)
