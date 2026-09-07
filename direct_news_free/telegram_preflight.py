#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only Telegram readiness check. Never prints tokens or chat IDs."""
import json
import os
import urllib.request


token = os.getenv("TELEGRAM_BOT_TOKEN_V2", "").strip()
primary = os.getenv("TELEGRAM_PRIMARY_CHAT_ID", "").strip()
mirror = os.getenv("TELEGRAM_MIRROR_CHAT_ID", "").strip()

if not token:
    raise SystemExit("TELEGRAM_PREFLIGHT token_present=false")


def api(method):
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/{method}", timeout=15
    ) as response:
        return json.load(response)


me = api("getMe")
if not me.get("ok"):
    raise SystemExit("TELEGRAM_PREFLIGHT getMe_failed")
bot = me.get("result") or {}
print(
    "TELEGRAM_PREFLIGHT token_present=true bot_name=%r bot_username=%r"
    % (bot.get("first_name", ""), bot.get("username", "")),
    flush=True,
)

explicit_ready = bool(primary and mirror and primary != mirror)
starts = set()
if not explicit_ready:
    updates = api("getUpdates")
    for update in updates.get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or chat.get("id") is None:
            continue
        if not str(message.get("text", "") or "").strip().casefold().startswith("/start"):
            continue
        starts.add(str(chat["id"]))

print(
    "TELEGRAM_PREFLIGHT explicit_pair=%s private_start_chats=%d ready=%s"
    % (str(explicit_ready).lower(), len(starts), str(explicit_ready or len(starts) >= 2).lower()),
    flush=True,
)
if not (explicit_ready or len(starts) >= 2):
    raise SystemExit("TELEGRAM_PREFLIGHT recipients_not_ready")
