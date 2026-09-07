#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hardened entrypoint for the Railway-free direct-news watcher.

Adds durable encrypted Telegram-recipient persistence on top of runner.py. The
public repository stores only ciphertext; the GitHub Actions bot-token secret is
required to decrypt it. This removes the long-term dependency on Telegram's
short-lived getUpdates queue while keeping chat IDs out of plaintext state.
"""
import asyncio

import aiohttp

import recipient_vault
import runner


watcher = runner.watcher
_original_discover = runner.discover_recipients_private


async def discover_recipients_persistent(session, state):
    # First prefer explicit Actions secrets if the owner adds them later.
    primary = runner.os.getenv("TELEGRAM_PRIMARY_CHAT_ID", "").strip()
    mirror = runner.os.getenv("TELEGRAM_MIRROR_CHAT_ID", "").strip()
    if primary and mirror and primary != mirror:
        return primary, mirror

    # Otherwise use the encrypted durable pair already committed in state.
    pair = recipient_vault.read_from_state(state, watcher.TOKEN)
    if pair:
        return pair

    # One-time discovery from fresh /start updates, then immediately seal it.
    pair = await _original_discover(session, state)
    if pair and watcher.TOKEN:
        recipient_vault.write_to_state(state, watcher.TOKEN, pair[0], pair[1])
        watcher.save_state(state)
        watcher.log.info("Telegram recipient vault ready (encrypted; IDs not logged)")
    return pair


watcher.discover_recipients = discover_recipients_persistent


async def main():
    # Build the encrypted vault even in shadow mode, before Railway is stopped.
    # This is read-only with Telegram and sends no message.
    if watcher.TOKEN:
        state = watcher.load_state()
        connector = aiohttp.TCPConnector(limit=2, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            pair = await discover_recipients_persistent(session, state)
            if pair:
                watcher.save_state(state)
            else:
                watcher.log.warning("Telegram recipient vault is not ready")

    await watcher.main()


if __name__ == "__main__":
    asyncio.run(main())
