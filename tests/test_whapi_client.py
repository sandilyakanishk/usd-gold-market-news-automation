"""Whapi client with the network replaced. No test here contacts Whapi."""

import io
import json
import urllib.error

import pytest

from src.delivery import whapi
from src.delivery.whapi import (
    WHAPI_USER_AGENT, WhapiAuthError, WhapiClient, WhapiConfigError, WhapiDestinationError, WhapiError,
    WhapiMessageError, WhapiNetworkError, WhapiSessionError, mask_chat_id,
)

TOKEN = "tok-SECRET-abcdef0123456789"
CHAT = "120363000000008282@g.us"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def patch(monkeypatch, body=None, error=None, calls=None):
    def urlopen(request, timeout=None):
        if calls is not None:
            calls.append((request, timeout))
        if error is not None:
            raise error
        return FakeResponse(json.dumps(body).encode("utf-8") if not isinstance(body, str) else body.encode("utf-8"))
    monkeypatch.setattr(whapi.urllib.request, "urlopen", urlopen)


def http_error(code, body):
    return urllib.error.HTTPError(f"https://gate.whapi.cloud/x?token={TOKEN}", code, "err", None,
                                  io.BytesIO((json.dumps(body) if not isinstance(body, str) else body).encode("utf-8")))


SENT_OK = {"sent": True, "message": {"id": "PsqXn5SAD5v7HRA-wHqB9tMeGQ", "type": "text", "chat_id": CHAT}}


def test_send_text_posts_the_exact_text_and_returns_the_message_id(monkeypatch):
    calls = []
    patch(monkeypatch, SENT_OK, calls=calls)
    text = "📅 *USD + GOLD DAILY UPDATE*\n━━━━━━━━━━━━━━━━━━\n*CPI m/m*\nForecast: 0.3%\n_note_"
    assert WhapiClient(TOKEN, timeout=7).send_text(CHAT, text) == "PsqXn5SAD5v7HRA-wHqB9tMeGQ"
    (request, timeout), = calls
    assert (request.get_method(), request.full_url, timeout) == ("POST", "https://gate.whapi.cloud/messages/text", 7)
    assert json.loads(request.data.decode("utf-8")) == {"to": CHAT, "body": text}   # byte for byte, nothing added
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("Content-type") == "application/json"
    assert TOKEN not in request.full_url


def test_every_request_uses_the_project_user_agent(monkeypatch):
    calls = []
    patch(monkeypatch, {"status": {"code": 4, "text": "AUTH"}}, calls=calls)
    WhapiClient(TOKEN).health()
    patch(monkeypatch, SENT_OK, calls=calls)
    WhapiClient(TOKEN).send_text(CHAT, "x")
    patch(monkeypatch, {"announceGroupInfo": {"id": CHAT}}, calls=calls)
    WhapiClient(TOKEN).announcement_group_id("120363000000001940@g.us")
    agents = [request.get_header("User-agent") for request, _ in calls]
    assert agents == [WHAPI_USER_AGENT] * 3
    assert "usd-gold-calendar-collector" in WHAPI_USER_AGENT and "python-urllib" not in WHAPI_USER_AGENT.lower()


@pytest.mark.parametrize("configured, sent", [
    (None, WHAPI_USER_AGENT), ("", WHAPI_USER_AGENT), ("Python-urllib/3.11", WHAPI_USER_AGENT),
    ("my-own-agent/2.0", "my-own-agent/2.0"),
])
def test_generic_python_agent_is_never_sent(monkeypatch, configured, sent):
    calls = []
    patch(monkeypatch, SENT_OK, calls=calls)
    WhapiClient(TOKEN, user_agent=configured).send_text(CHAT, "x")
    assert calls[0][0].get_header("User-agent") == sent


def test_health(monkeypatch):
    calls = []
    patch(monkeypatch, {"status": {"code": 4, "text": "AUTH"}, "uptime": 10}, calls=calls)
    assert WhapiClient(TOKEN).health() == {"status": "AUTH"}
    assert (calls[0][0].get_method(), calls[0][0].full_url) == ("GET", "https://gate.whapi.cloud/health")
    for text in ("QR", "INIT", "LAUNCH", "STOP", ""):
        patch(monkeypatch, {"status": {"code": 2, "text": text}})
        with pytest.raises(WhapiSessionError, match="not connected"):
            WhapiClient(TOKEN).health()


def test_announcement_group_lookup(monkeypatch):
    calls = []
    patch(monkeypatch, {"announceGroupInfo": {"title": "Usd test", "id": CHAT}, "otherGroups": [{"id": "x@g.us"}]}, calls=calls)
    assert WhapiClient(TOKEN).announcement_group_id("120363000000001940@g.us") == CHAT
    assert calls[0][0].full_url == "https://gate.whapi.cloud/communities/120363000000001940@g.us/subgroups"
    patch(monkeypatch, {"otherGroups": []})
    assert WhapiClient(TOKEN).announcement_group_id("120363000000001940@g.us") is None


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_token(token):
    with pytest.raises(WhapiConfigError, match="WHAPI_TOKEN is not set"):
        WhapiClient(token)


@pytest.mark.parametrize("chat_id", [None, "", "  "])
def test_missing_destination(monkeypatch, chat_id):
    calls = []
    patch(monkeypatch, SENT_OK, calls=calls)
    with pytest.raises(WhapiConfigError, match="WHATSAPP_ANNOUNCEMENT_CHAT_ID is not set"):
        WhapiClient(TOKEN).send_text(chat_id, "hello")
    assert calls == []


@pytest.mark.parametrize("text, message", [("", "no text"), ("   \n ", "no text"), (None, "no text"), ("x" * 4097, "limit is 4096")])
def test_unsendable_text_is_rejected_before_any_request(monkeypatch, text, message):
    calls = []
    patch(monkeypatch, SENT_OK, calls=calls)
    with pytest.raises(WhapiMessageError, match=message):
        WhapiClient(TOKEN).send_text(CHAT, text)
    assert calls == []


@pytest.mark.parametrize("error, expected, text", [
    (http_error(401, {"error": {"code": 401, "message": f"Invalid token {TOKEN}"}}), WhapiAuthError, "Check WHAPI_TOKEN"),
    (http_error(403, {"error": {"code": 403, "message": "Forbidden"}}), WhapiAuthError, "Check WHAPI_TOKEN"),
    (http_error(404, {"error": {"code": 404, "message": "Chat not found"}}), WhapiDestinationError, "WHATSAPP_ANNOUNCEMENT_CHAT_ID"),
    (http_error(400, {"error": {"code": 400, "message": "recipient is not valid"}}), WhapiDestinationError, "HTTP 400"),
    (http_error(503, {"error": {"code": 503, "message": "Channel is not ready"}}), WhapiSessionError, "still linked"),
    (http_error(500, {"error": {"code": 500, "message": "session expired"}}), WhapiSessionError, "still linked"),
    (http_error(500, "<html>oops</html>"), WhapiError, "HTTP 500"),
    (http_error(429, {"message": "Too many requests"}), WhapiError, "HTTP 429"),
    (http_error(403, {"title": "Error 1010: Access denied", "detail": "browser signature banned"}), WhapiError, "HTTP 403"),
    (urllib.error.URLError("getaddrinfo failed"), WhapiNetworkError, "Could not reach Whapi"),
    (TimeoutError("timed out"), WhapiNetworkError, "Could not reach Whapi"),
    (ConnectionResetError("reset"), WhapiNetworkError, "Could not reach Whapi"),
])
def test_failures_are_classified_and_never_leak_the_token(monkeypatch, error, expected, text):
    patch(monkeypatch, error=error)
    with pytest.raises(expected) as raised:
        WhapiClient(TOKEN).send_text(CHAT, "hello")
    message = str(raised.value)
    assert text in message and TOKEN not in message and "gate.whapi.cloud/x" not in message


@pytest.mark.parametrize("body", [
    {"sent": False, "error": "blocked"}, {"sent": True}, {"sent": True, "message": {}}, {"message": "queued"}, {},
])
def test_unconfirmed_send_is_an_error(monkeypatch, body):
    patch(monkeypatch, body)
    with pytest.raises(WhapiError, match="did not confirm"):
        WhapiClient(TOKEN).send_text(CHAT, "hello")


@pytest.mark.parametrize("body", ["not json", "[1, 2]"])
def test_unexpected_responses_are_errors(monkeypatch, body):
    patch(monkeypatch, body)
    with pytest.raises(WhapiError):
        WhapiClient(TOKEN).send_text(CHAT, "hello")


def test_chat_ids_are_masked_for_display():
    assert mask_chat_id(CHAT) == "...8282@g.us"
    assert mask_chat_id("919999912345@s.whatsapp.net") == "...2345@s.whatsapp.net"
    assert mask_chat_id(None) == "(not set)" and mask_chat_id("") == "(not set)"
    assert "1203630000" not in mask_chat_id(CHAT)
