#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production runner wrapper with fallback coverage and private Telegram routing.

Some publishers reject GitHub-hosted IPs (403/429/503), return a challenge
response, or render no article anchors server-side. We never remove those
sources. When their direct page fails or yields zero links, this wrapper scans a
site-scoped Google News RSS feed for that exact publisher and Panathinaikos.

This repository is public so GitHub-hosted standard runners remain free. Telegram
recipient IDs are therefore never written to the committed state file: they are
read from Actions secrets when supplied, or discovered in memory from /start
updates for the current run only.
"""
import asyncio
import os
from urllib.parse import quote, urlparse

import feedparser

import watcher


_original_process_source = watcher.process_source
_original_load_state = watcher.load_state
FALLBACK_MAX_ITEMS = 80


def load_state_without_private_recipients():
    state = _original_load_state()
    # Never persist Telegram chat IDs in this public repository.
    state["recipients"] = {}
    return state


async def discover_recipients_private(session, state):
    primary = os.getenv("TELEGRAM_PRIMARY_CHAT_ID", "").strip()
    mirror = os.getenv("TELEGRAM_MIRROR_CHAT_ID", "").strip()
    if primary and mirror and primary != mirror:
        return primary, mirror

    if not watcher.TOKEN:
        return None

    # Read-only fallback discovery. Do not write the discovered IDs to state.
    try:
        async with session.get(
            f"https://api.telegram.org/bot{watcher.TOKEN}/getUpdates",
            timeout=watcher.aiohttp.ClientTimeout(total=watcher.HTTP_TIMEOUT),
        ) as response:
            payload = await response.json(content_type=None)
        starts = []
        for update in payload.get("result", []):
            message = update.get("message") or update.get("edited_message")
            chat = (message or {}).get("chat") or {}
            if not message or chat.get("type") != "private" or chat.get("id") is None:
                continue
            if not str(message.get("text", "") or "").strip().casefold().startswith("/start"):
                continue
            starts.append((int(update.get("update_id", 0) or 0), str(chat["id"])))
        latest = {}
        for update_id, chat_id in sorted(starts):
            latest[chat_id] = update_id
        ordered = sorted(latest, key=lambda chat_id: latest[chat_id])
        if len(ordered) >= 2:
            return ordered[-1], ordered[-2]
    except Exception as exc:
        watcher.log.warning("Private Telegram recipient discovery failed: %s", exc)
    return None


def _domain(source):
    return (urlparse(source.url).hostname or "").lower().removeprefix("www.")


def _feed_url(source):
    domain = _domain(source)
    query = f'site:{domain} ("Παναθηναϊκός" OR "Παναθηναϊκό" OR "Panathinaikos" OR "PAOBC") when:1d'
    return (
        "https://news.google.com/rss/search?"
        f"q={quote(query)}&hl=el&gl=GR&ceid=GR:el"
    )


def _entries(body, source):
    feed = feedparser.parse(body)
    out = []
    for entry in list(feed.entries)[:FALLBACK_MAX_ITEMS]:
        url = watcher.normalize_url(getattr(entry, "link", "") or "")
        if not url:
            continue
        title = getattr(entry, "title", "") or ""
        published = (
            getattr(entry, "published", "")
            or getattr(entry, "updated", "")
            or ""
        )
        out.append(
            watcher.Item(
                source=f"{source.name} · fallback",
                url=url,
                title=title,
                published=published,
            )
        )
    return out


async def _fallback_source(session, state, source, health):
    slot = health["sources"].setdefault(source.name, {})
    body, _final, status = await watcher.fetch(session, _feed_url(source))
    slot["fallback_last_attempt"] = watcher.now_iso()
    slot["fallback_http_status"] = status
    if not body:
        slot["fallback_status"] = "error"
        slot["fallback_last_error"] = f"Google News fallback HTTP {status}"
        return

    items = _entries(body, source)
    first = source.name not in state["initialized_sources"]
    sent = 0

    for item in reversed(items):
        if watcher.is_seen(state, item.url):
            continue

        # The site-scoped PAO query itself is the relevance gate. Keep the same
        # freshness policy and never mark a failed production delivery as seen.
        if first or not watcher.DELIVERY_ENABLED:
            watcher.mark_seen(state, item)
            continue

        if not watcher.is_recent(item.published, hours=24, unknown_ok=True):
            watcher.mark_seen(state, item)
            continue

        try:
            await watcher.send_alert(session, state, item)
        except Exception as exc:
            slot["fallback_last_error"] = f"delivery: {type(exc).__name__}: {exc}"
            continue

        watcher.mark_seen(state, item)
        sent += 1

    if first:
        state["initialized_sources"].append(source.name)
    watcher.save_state(state)

    slot.update(
        {
            "fallback_status": "ok",
            "fallback_last_ok": watcher.now_iso(),
            "fallback_items": len(items),
            "fallback_sent": int(slot.get("fallback_sent", 0) or 0) + sent,
            "fallback_last_error": None,
        }
    )


async def process_source_with_fallback(session, state, source, health):
    await _original_process_source(session, state, source, health)
    slot = health["sources"].setdefault(source.name, {})
    direct_bad = slot.get("status") != "ok" or int(slot.get("visible_items", 0) or 0) == 0
    if not direct_bad:
        slot["coverage"] = "direct"
        return

    try:
        await _fallback_source(session, state, source, health)
    except Exception as exc:
        slot["fallback_status"] = "error"
        slot["fallback_last_error"] = f"{type(exc).__name__}: {exc}"

    if slot.get("fallback_status") == "ok":
        slot["coverage"] = "fallback"
        # A blocked direct endpoint is still recorded, but coverage is healthy.
        slot["effective_status"] = "ok"
    else:
        slot["coverage"] = "unresolved"
        slot["effective_status"] = "error"


watcher.load_state = load_state_without_private_recipients
watcher.discover_recipients = discover_recipients_private
watcher.process_source = process_source_with_fallback


if __name__ == "__main__":
    asyncio.run(watcher.main())
