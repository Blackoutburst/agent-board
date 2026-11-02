import json
import os
from pathlib import Path
import stat
import sys

import httpx
import pytest

import manage


BOT = "1464251071852445823"
GUILD = "123456789012345678"
CHANNEL = "234567890123456789"
TOKEN = "Brokie-token-test-secret"


def transport(identity=None, channel=None, identity_status=200, channel_status=200):
    identity = identity if identity is not None else {"id": BOT, "bot": True}
    channel = channel if channel is not None else {"id": CHANNEL, "guild_id": GUILD, "type": 0}
    def respond(request):
        assert request.headers["Authorization"] == "Bot " + TOKEN
        assert request.url.host == "discord.com"
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(identity_status, json=identity)
        assert request.url.path == "/api/v10/channels/" + CHANNEL
        return httpx.Response(channel_status, json=channel)
    return httpx.MockTransport(respond)


def configure(tmp_path, **kwargs):
    values = {"data_dir": tmp_path / "data", "token": TOKEN, "bot_id": BOT,
              "guild_id": GUILD, "channel_id": CHANNEL, "transport": transport()}
    values.update(kwargs)
    return manage.configure_discord(**values)


@pytest.mark.parametrize("channel_type", [0, 5])
def test_valid_bot_and_destination_write_private_configuration(tmp_path, channel_type):
    result = configure(tmp_path, transport=transport(channel={"id": CHANNEL, "guild_id": GUILD, "type": channel_type}))
    path = Path(result["config_path"])
    config = json.loads(path.read_text())
    assert config["bot_token"] == TOKEN
    assert config["bot_id"] == BOT and config["guild_id"] == GUILD and config["channel_id"] == CHANNEL
    assert config["enabled_since"] > 0 and config["user_ids"] == {}
    assert TOKEN not in json.dumps(result)
    assert not list(path.parent.glob(".discord-notifications-*"))
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("identity", [{"id": "999", "bot": True}, {"id": BOT, "bot": False}, {"id": BOT}])
def test_identity_mismatch_never_replaces_existing_configuration(tmp_path, identity):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "discord-notifications.json"
    old = b'{"bot_token":"existing-private-credential"}'
    path.write_bytes(old)
    with pytest.raises(ValueError) as caught:
        configure(tmp_path, transport=transport(identity=identity))
    assert TOKEN not in str(caught.value)
    assert path.read_bytes() == old
    assert not list(data.glob(".discord-notifications-*"))


@pytest.mark.parametrize("channel", [
    {"id": CHANNEL, "guild_id": "999", "type": 0},
    {"id": "999", "guild_id": GUILD, "type": 0},
    {"id": CHANNEL, "guild_id": GUILD, "type": 2},
    {"id": CHANNEL, "guild_id": GUILD, "type": False},
])
def test_destination_mismatch_preserves_configuration(tmp_path, channel):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "discord-notifications.json"
    path.write_text("previous configuration")
    with pytest.raises(ValueError):
        configure(tmp_path, transport=transport(channel=channel))
    assert path.read_text() == "previous configuration"


def test_permission_failure_is_generic_and_does_not_write(tmp_path):
    with pytest.raises(ValueError) as caught:
        configure(tmp_path, transport=transport(channel_status=403, channel={"error": TOKEN}))
    assert str(caught.value) == "The Discord bot cannot access the configured channel"
    assert TOKEN not in str(caught.value)
    assert not (tmp_path / "data" / "discord-notifications.json").exists()


def test_http_error_does_not_disclose_token_or_exception_detail(tmp_path):
    def error(request):
        raise httpx.ConnectError("Sensitive failure includes " + TOKEN, request=request)
    with pytest.raises(ValueError) as caught:
        configure(tmp_path, transport=httpx.MockTransport(error))
    assert str(caught.value) == "Discord connection unavailable; configuration was not changed"
    assert TOKEN not in str(caught.value)
    assert caught.value.__suppress_context__
    assert not (tmp_path / "data" / "discord-notifications.json").exists()


def test_matching_destination_preserves_only_owner_user_ids(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "discord-notifications.json"
    path.write_text(json.dumps({"guild_id": GUILD, "channel_id": CHANNEL,
                               "user_ids": {"blackoutburst": "345678901234567890", "thescreamer": "invalid", "outsider": "456"}}))
    configure(tmp_path)
    assert json.loads(path.read_text())["user_ids"] == {"blackoutburst": "345678901234567890"}


def test_cli_hidden_prompt_does_not_seed_board_or_print_credential(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manage.py", "--data-dir", str(tmp_path / "data"), "configure-discord",
                                     "--bot-id", BOT, "--guild-id", GUILD, "--channel-id", CHANNEL])
    monkeypatch.setattr(manage.getpass, "getpass", lambda prompt: TOKEN)
    real = manage.configure_discord
    monkeypatch.setattr(manage, "configure_discord", lambda *args: real(*args, transport=transport()))
    def forbidden(*args, **kwargs):
        pytest.fail("Discord setup must not initialize or seed the board")
    monkeypatch.setattr(manage, "Board", forbidden)
    manage.main()
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert BOT in captured.out
    assert (tmp_path / "data" / "discord-notifications.json").is_file()
