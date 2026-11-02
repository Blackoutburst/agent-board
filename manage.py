"""Local administration: development passwords, Discord setup, and games examples."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import tempfile
import time
import warnings

import httpx

from board import Board

OWNERS = {"blackoutburst": "Blackoutburst", "thescreamer": "TheScreamer"}


def configure_discord(data_dir, token, bot_id, guild_id, channel_id, transport=None):
    """Validate the chosen bot and destination before replacing its private credential."""
    for identifier in (bot_id, guild_id, channel_id):
        if not isinstance(identifier, str) or not identifier.isascii() or not identifier.isdigit():
            raise ValueError("Discord bot, guild, and channel IDs must be numeric")
    if (not isinstance(token, str) or not token or len(token) > 1024
            or any(not character.isascii() or not character.isprintable() or character.isspace() for character in token)):
        raise ValueError("A valid Discord bot token is required")
    try:
        with httpx.Client(base_url="https://discord.com/api/v10", timeout=15, trust_env=False,
                          follow_redirects=False, transport=transport,
                          headers={"Authorization": "Bot " + token}) as client:
            identity_response = client.get("/users/@me")
            if identity_response.status_code != 200:
                raise ValueError("Discord credentials could not be validated")
            identity = identity_response.json()
            if not isinstance(identity, dict) or identity.get("bot") is not True or identity.get("id") != bot_id:
                raise ValueError("The supplied token does not belong to the expected Discord bot")
            channel_response = client.get("/channels/" + channel_id)
            if channel_response.status_code != 200:
                raise ValueError("The Discord bot cannot access the configured channel")
            channel = channel_response.json()
            if (not isinstance(channel, dict) or channel.get("id") != channel_id
                    or channel.get("guild_id") != guild_id
                    or type(channel.get("type")) is not int or channel["type"] not in (0, 5)):
                raise ValueError("The Discord destination is not a text channel in the expected guild")
    except httpx.HTTPError:
        raise ValueError("Discord connection unavailable; configuration was not changed") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Discord returned an invalid response; configuration was not changed") from None

    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = data_dir / "discord-notifications.json"
    user_ids = {}
    try:
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if (isinstance(existing, dict) and existing.get("guild_id") == guild_id
                and existing.get("channel_id") == channel_id and isinstance(existing.get("user_ids"), dict)):
            user_ids = {owner: str(identifier) for owner, identifier in existing["user_ids"].items()
                        if owner in OWNERS and str(identifier).isascii() and str(identifier).isdigit()}
    except (OSError, ValueError):
        pass
    configuration = {"bot_token": token, "bot_id": bot_id, "guild_id": guild_id,
                     "channel_id": channel_id, "enabled_since": time.time(), "user_ids": user_ids}
    descriptor, temporary = tempfile.mkstemp(prefix=".discord-notifications-", suffix=".json", dir=data_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(configuration, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"bot_id": bot_id, "guild_id": guild_id, "channel_id": channel_id,
            "channel_url": f"https://discord.com/channels/{guild_id}/{channel_id}",
            "config_path": str(destination)}


def seed_games(board: Board, creator: str) -> dict:
    for owner_id, display_name in OWNERS.items():
        board.ensure_owner(owner_id, display_name)
    actor = {"kind": "human", "owner_id": creator}
    thread = board.create_thread(actor, "Games rework — OAuth and email setup",
                                 list(OWNERS), "seed-games-thread-v1")
    thread_id = thread["id"]
    board.post_message(
        actor, thread_id,
        "Example handoff: Google project ownership is unknown. Domain ownership does not "
        "establish Google account ownership. Confirm the responsible human and derive "
        "the exact OAuth origins/callback URLs from the application before requesting "
        "client creation. For email, obtain the provider's exact records and domain; "
        "do not invent SPF, DKIM, DMARC, TXT or CNAME values. Keep client secrets, "
        "passwords and provider tokens in application secret storage. Agent policies "
        "remain disabled until each owner explicitly opts in.",
        idempotency_key="seed-games-message-v1")
    tasks = []
    tasks.append(board.create_task(
        actor, thread_id, "Confirm the Google OAuth project owner and requirements",
        "TheScreamer: identify the intended Google project/account owner. Derive "
        "the exact authorized origins and callback URLs from the games application. "
        "After that human confirms ownership, create a separate client-configuration "
        "task addressed to that human. Do not assume Blackoutburst owns the Google project.",
        "Named responsible human; confirmed Google project; exact origins and callback "
        "URLs; required consent-screen settings. Credentials belong in secret storage.",
        "human", "thescreamer", idempotency_key="seed-games-oauth-owner-v1"))
    records = board.create_task(
        actor, thread_id, "Supply the email provider's exact DNS requirements",
        "TheScreamer: obtain the actual sending domain and provider verification records. "
        "Post record type, hostname, value and any specified TTL. Identify existing "
        "SPF policy before proposing a change. Never post an email API key.",
        "Provider name; actual sending domain; exact records; verification procedure; "
        "existing-record evidence. No fabricated DNS values.",
        "human", "thescreamer", idempotency_key="seed-games-email-records-v1")
    tasks.append(records)
    dns = board.create_task(
        actor, thread_id, "Add the agreed email DNS records",
        "Blackoutburst: after the requirements task is verified, inspect existing records "
        "and apply the agreed changes through the domain account you own. Record "
        "what changed and when, then request verification. Provider acceptance and "
        "a successful application email test are separate checks.",
        "Exact records supplied by the provider; existing-record review; human "
        "confirmation of the actual sending-domain ownership and changes.",
        "human", "blackoutburst", prerequisites=[records["id"]],
        idempotency_key="seed-games-email-dns-v1")
    tasks.append(dns)
    tasks.append(board.create_task(
        actor, thread_id, "Verify the published email DNS records",
        "After Blackoutburst reports the DNS change, compare public DNS responses to the "
        "supplied provider records. Record resolver, query names, responses and time. "
        "If the email provider has not accepted verification, state that explicitly; "
        "do not report an application email test as passed without running it.",
        "Verified DNS change prerequisite and owner authorization for read-only "
        "public DNS checks. Distinguish DNS visibility, provider acceptance and "
        "application email delivery.",
        "group", "blackoutburst", action="verify_dns", resource="games-email-dns",
        required_capabilities=["dns_public"], prerequisites=[dns["id"]],
        idempotency_key="seed-games-email-verification-v1"))
    return {"thread": thread, "tasks": tasks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("BOARD_DATA_DIR", "./data")))
    sub = parser.add_subparsers(dest="command", required=True)
    password = sub.add_parser("owner-password", help="Set a development-only local password")
    password.add_argument("owner", choices=OWNERS)
    seed = sub.add_parser("seed-games", help="Create the optional example once (safe to retry)")
    seed.add_argument("--creator", choices=OWNERS, default="thescreamer")
    discord = sub.add_parser("configure-discord", help="Validate and save a Discord bot token through a hidden prompt")
    discord.add_argument("--bot-id", required=True)
    discord.add_argument("--guild-id", required=True)
    discord.add_argument("--channel-id", required=True)
    args = parser.parse_args()
    if args.command == "configure-discord":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("Discord bot token (hidden): ")
            result = configure_discord(args.data_dir, token, args.bot_id, args.guild_id, args.channel_id)
        except (getpass.GetPassWarning, EOFError):
            parser.error("A secure interactive terminal is required for the hidden token prompt")
        except (ValueError, OSError) as error:
            parser.error(str(error))
        print(json.dumps(result, indent=2))
        return
    board = Board(args.data_dir, base_url=os.environ.get("BOARD_BASE_URL", ""))
    if args.command == "owner-password":
        first = getpass.getpass("New development password: ")
        second = getpass.getpass("Repeat password: ")
        if len(first) < 12:
            parser.error("Use at least 12 characters")
        if first != second:
            parser.error("Passwords differ")
        board.ensure_owner(args.owner, OWNERS[args.owner], password=first)
        print("Development password set. Local login also requires BOARD_ALLOW_LOCAL_PASSWORDS=1.")
    elif args.command == "seed-games":
        result = seed_games(board, args.creator)
        print(json.dumps({"thread_id": result["thread"]["id"],
                          "task_ids": [task["id"] for task in result["tasks"]]}, indent=2))


if __name__ == "__main__":
    main()
