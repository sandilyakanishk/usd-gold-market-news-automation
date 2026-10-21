"""MT5 trade alerts: the receiver's messages, its secret check, and the add-on's events."""

import importlib.util
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src import trade_alerts
from src.config import PROJECT_ROOT

_spec = importlib.util.spec_from_file_location("trade_receiver", PROJECT_ROOT / "web" / "trade-alerts" / "api" / "trade.py")
receiver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(receiver)

NOW = datetime(2026, 10, 21, 14, 12, tzinfo=timezone.utc)          # 7:42 PM IST
SECRET = "s3cret-for-tests-0123456789"
ENV = {"TRADE_WEBHOOK_SECRET": SECRET, "TELEGRAM_BOT_TOKEN": "123:TEST", "TELEGRAM_CHAT_ID": "@example",
       "WHAPI_TOKEN": "whapi-TEST", "WHATSAPP_CHAT_ID": "120363000000005555@newsletter"}
EA = (PROJECT_ROOT / "web" / "trade-alerts" / "mt5" / "TradeAlerts.mq5").read_text(encoding="utf-8")


def msg(**event):
    return receiver.build_message(event, NOW)


# -- messages ------------------------------------------------------------------------------

def test_every_kind_of_trade_post():
    assert msg(event="opened", symbol="XAUUSD", side="BUY", price=4120.5, sl=4110, tp=4140).split("\n")[:3] == [
        "🟢 *BUY XAUUSD* @ 4,120.50", "SL 4,110.00 · TP 4,140.00", "🕒 7:42 PM IST"]
    assert msg(event="opened", symbol="XAUUSD", side="SELL", price=4120.5, sl=0, tp=0).startswith(
        "🔴 *SELL XAUUSD* @ 4,120.50\nSL none · TP none")
    assert msg(event="pending_placed", symbol="XAUUSD", side="BUY LIMIT", price=4110, sl=4100, tp=4140).startswith(
        "⏳ *PENDING ORDER* · XAUUSD\nBUY LIMIT @ 4,110.00\nSL 4,100.00 · TP 4,140.00")
    assert msg(event="pending_cancelled", symbol="XAUUSD", side="SELL STOP", price=4090).startswith(
        "🚫 *ORDER CANCELLED* · XAUUSD\nSELL STOP @ 4,090.00 has been cancelled.")
    assert "SL 4,120.50 · TP 4,140.00 (breakeven)" in msg(event="modified", symbol="XAUUSD", side="BUY", price=4120.5, sl=4120.5,
                                                         tp=4140, open_price=4120.5)
    tp = msg(event="closed", symbol="XAUUSD", side="BUY", open_price=4120.5, close_price=4140, points=1950, reason="tp")
    assert tp.startswith("✅ *TP HIT* · XAUUSD BUY\nEntry 4,120.50 → Exit 4,140.00\nResult: +19.50 (+1,950 points)")
    sl = msg(event="closed", symbol="XAUUSD", side="SELL", open_price=4120.5, close_price=4130, points=-950, reason="sl")
    assert sl.startswith("❌ *SL HIT* · XAUUSD SELL") and "Result: -9.50 (-950 points)" in sl
    assert msg(event="closed", symbol="XAUUSD", side="BUY", open_price=1, close_price=1.1, reason="manual").startswith("✋ *CLOSED MANUALLY*")
    assert msg(event="closed", symbol="XAUUSD", side="BUY", open_price=1, close_price=1.1, reason="partial").startswith("➗ *PARTIAL CLOSE*")
    assert msg(event="opened", symbol="EURUSD", side="BUY", price=1.08345, sl=1.082, tp=1.0865).startswith("🟢 *BUY EURUSD* @ 1.08345")


def test_no_lot_size_or_money_ever_appears_and_every_post_carries_the_disclaimer():
    text = msg(event="opened", symbol="XAUUSD", side="BUY", price=4120.5, sl=4110, tp=4140, volume=5, profit=900, balance=10000)
    assert "5" not in text.split("\n")[0].replace("4,120.50", "") and "900" not in text and "10000" not in text
    assert text.endswith(receiver.DISCLAIMER)


@pytest.mark.parametrize("event", [
    "not an object", {"event": "hacked", "symbol": "XAUUSD", "side": "BUY", "price": 1},
    {"event": "opened", "symbol": "", "side": "BUY", "price": 1}, {"event": "opened", "symbol": "XAU<script>", "side": "BUY", "price": 1},
    {"event": "opened", "symbol": "XAUUSD", "side": "BUY LIMIT", "price": 1}, {"event": "pending_placed", "symbol": "XAUUSD", "side": "BUY", "price": 1},
    {"event": "opened", "symbol": "XAUUSD", "side": "BUY"}, {"event": "opened", "symbol": "XAUUSD", "side": "BUY", "price": "abc"},
    {"event": "closed", "symbol": "XAUUSD", "side": "BUY", "open_price": 1},
])
def test_odd_events_are_refused(event):
    with pytest.raises(receiver.BadEvent):
        receiver.build_message(event, NOW)


# -- the request handler -------------------------------------------------------------------

def senders(log, telegram_ok=True, whatsapp_ok=True):
    return (lambda text, env: log.append(("telegram", text)) or (telegram_ok, "HTTP 200"),
            lambda text, env: log.append(("whatsapp", text)) or (whatsapp_ok, "HTTP 200"))


def body(**event):
    return json.dumps(event).encode()


def test_a_trade_goes_to_both_channels_with_the_same_text():
    log = []
    status, reply = receiver.handle(body(event="opened", symbol="XAUUSD", side="BUY", price=4120.5, sl=4110, tp=4140), SECRET, ENV,
                                    now=NOW, senders=senders(log))
    assert status == 200 and reply["ok"] and [c for c, _ in log] == ["telegram", "whatsapp"] and log[0][1] == log[1][1]


def test_without_the_right_secret_nothing_is_posted():
    log = []
    for wrong in ("", "nope", SECRET[:-1]):
        status, _ = receiver.handle(body(event="opened", symbol="XAUUSD", side="BUY", price=1), wrong, ENV, senders=senders(log))
        assert status == 401
    assert log == []
    assert receiver.handle(b"{}", SECRET, {**ENV, "TRADE_WEBHOOK_SECRET": "short"}, senders=senders(log))[0] == 503
    assert receiver.handle(b"x" * 20_000, SECRET, ENV, senders=senders(log))[0] == 413
    assert receiver.handle(b"not json", SECRET, ENV, senders=senders(log))[0] == 400 and log == []


def test_one_channel_failing_does_not_stop_the_other():
    log = []
    status, reply = receiver.handle(body(event="opened", symbol="XAUUSD", side="BUY", price=1), SECRET, ENV, now=NOW,
                                    senders=senders(log, whatsapp_ok=False))
    assert status == 200 and reply["telegram"]["sent"] and not reply["whatsapp"]["sent"]
    assert receiver.handle(body(event="opened", symbol="XAUUSD", side="BUY", price=1), SECRET, ENV, now=NOW,
                           senders=senders([], False, False))[0] == 502


def test_secrets_never_leak_into_a_reply(monkeypatch):
    import io, urllib.error
    def refuse(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b"bad token 123:TEST whapi-TEST"))
    monkeypatch.setattr(receiver.urllib.request, "urlopen", refuse)
    _, reply = receiver.handle(body(event="opened", symbol="XAUUSD", side="BUY", price=1), SECRET, ENV, now=NOW)
    assert "123:TEST" not in json.dumps(reply) and "whapi-TEST" not in json.dumps(reply)


# -- the MT5 add-on ------------------------------------------------------------------------

def test_the_add_on_reports_each_kind_of_action_and_sends_no_money_or_size():
    for event in ("pending_placed", "pending_cancelled", "opened", "modified", "closed"):
        assert f'"{event}"' in EA or f'\\"event\\":\\"{event}\\"' in EA
    for reason in ('"tp"', '"sl"', '"stopout"', '"partial"', '"manual"'):
        assert reason in EA
    assert "ORDER_STATE_CANCELED" in EA and "X-Trade-Secret" in EA and "WebRequest" in EA
    for forbidden in ("DEAL_VOLUME", "POSITION_VOLUME", "DEAL_PROFIT", "POSITION_PROFIT", "ACCOUNT_BALANCE", "ACCOUNT_EQUITY"):
        assert forbidden not in EA
    assert 'input string WebhookSecret     = "";' in EA                  # the secret is never written into the file


# -- deployment ----------------------------------------------------------------------------

def test_the_secret_is_created_once_and_kept_in_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_CHAT_ID=@x\n", encoding="utf-8")
    environ = {}
    first = trade_alerts.ensure_secret(env_file, environ)
    assert len(first) >= 24 and f"TRADE_WEBHOOK_SECRET={first}" in env_file.read_text(encoding="utf-8")
    assert trade_alerts.ensure_secret(env_file, environ) == first


def test_deploy_sets_the_settings_and_publishes_only_the_receiver():
    log = []

    def call(method, path, token, payload=None):
        log.append((method, path, payload))
        if path.startswith("/v9/projects/"):
            return {"id": "prj_9"}
        if path == "/v13/deployments":
            return {"id": "dpl_1", "projectId": "prj_9"}
        if path.startswith("/v6/deployments"):
            return {"deployments": [{"uid": "dpl_1"}]}
        return {}
    settings = SimpleNamespace(telegram_bot_token="123:TEST", telegram_chat_id="@example", whapi_token="whapi-TEST",
                               whatsapp_announcement_chat_id="120363000000005555@newsletter", whapi_base_url="https://gate.whapi.cloud")
    address = trade_alerts.deploy(settings, "vercel-token", SECRET, call=call)
    assert address == "https://trade-alerts-owner.vercel.app"
    env_call = next(p for m, path, p in log if "/env" in path)
    assert {v["key"] for v in env_call} == {"TRADE_WEBHOOK_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WHAPI_TOKEN",
                                            "WHATSAPP_CHAT_ID", "WHAPI_BASE_URL"}
    assert all(v["type"] == "encrypted" for v in env_call)
    deployment = next(p for m, path, p in log if path == "/v13/deployments")
    assert {f["file"] for f in deployment["files"]} == {"api/trade.py", "requirements.txt"}
    assert "123:TEST" not in json.dumps(deployment) and SECRET not in json.dumps(deployment)
