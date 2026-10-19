"""Posting to a WhatsApp Channel (an id ending in @newsletter) through Whapi."""

import pytest

from src import main as cli
from src.delivery.whapi import WhapiClient, is_channel

CHANNEL = "120363000000005555@newsletter"
GROUP = "120363000000008282@g.us"


def test_channel_ids_are_told_from_group_ids():
    assert is_channel(CHANNEL) and is_channel(f" {CHANNEL} ")
    assert not is_channel(GROUP) and not is_channel("") and not is_channel(None)


def test_channel_role_reads_whapi(monkeypatch):
    client, calls = WhapiClient("tok-TEST"), []
    monkeypatch.setattr(client, "_request", lambda method, path, payload=None: calls.append((method, path)) or {"role": "owner"})
    assert client.channel_role(CHANNEL) == "owner"
    assert calls == [("GET", f"/newsletters/{CHANNEL}")]


class FakeWhapi:
    def __init__(self, role):
        self.role, self.sent = role, []

    def health(self):
        return {"status": "AUTH"}

    def channel_role(self, channel_id):
        return self.role

    def announcement_group_id(self, community_id):
        raise AssertionError("a Channel has no community to cross-check")

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return f"wamid-{len(self.sent)}"


def _env(monkeypatch, tmp_path, destination=CHANNEL):
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "log.txt"))
    monkeypatch.setenv("WHAPI_TOKEN", "tok-TEST")
    monkeypatch.setenv("WHATSAPP_ANNOUNCEMENT_CHAT_ID", destination)
    monkeypatch.setenv("WHATSAPP_COMMUNITY_ID", "120363000000001940@g.us")


@pytest.mark.parametrize("role, code", [("owner", 0), ("admin", 0), ("subscriber", 1), (None, 1)])
def test_the_check_confirms_this_number_may_post_in_the_channel(monkeypatch, tmp_path, capsys, role, code):
    _env(monkeypatch, tmp_path)
    fake = FakeWhapi(role)
    monkeypatch.setattr(cli, "build_whapi_client", lambda settings: fake)
    assert cli.main(["--whatsapp-check"]) == code
    captured = capsys.readouterr()
    assert fake.sent == [] and CHANNEL not in captured.out + captured.err      # only the masked id is ever shown
    if code:
        assert "must be its owner or an admin" in captured.err
    else:
        assert f"this number is its {role}" in captured.out and "Nothing was sent." in captured.out


def test_messages_go_to_the_channel(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    fake = FakeWhapi("owner")
    monkeypatch.setattr(cli, "build_whapi_client", lambda settings: fake)
    assert cli.main(["--whatsapp-test"]) == 0
    assert [chat for chat, _ in fake.sent] == [CHANNEL]
