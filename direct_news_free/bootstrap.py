#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fast shadow baseline for the Railway-free direct-news watcher.

During migration Railway remains authoritative for real Telegram delivery. This
bootstrap therefore records every article currently visible on every direct
source without opening article bodies or sending anything. New items appearing
after the baseline are then handled normally by watcher.py. This gives a fast,
loss-safe overlap without replaying old stories.
"""
import asyncio
import time

import aiohttp
import feedparser

import watcher


ALL_DIRECT = (
    watcher.CORE_SOURCES
    + watcher.RESTORED_SOURCES
    + watcher.INTL_PRIORITY_SOURCES
    + watcher.INTL_BROAD_SOURCES
)


async def baseline_source(session, state, source, health, sem):
    if source.name in state["initialized_sources"]:
        return
    started = time.time()
    async with sem:
        body, final, status = await watcher.fetch(session, source.url)
    slot = health["sources"].setdefault(source.name, {})
    slot.update({"last_attempt": watcher.now_iso(), "http_status": status})
    if not body:
        slot.update(
            {
                "status": "error",
                "last_error": f"baseline fetch failed HTTP {status}",
                "duration": round(time.time() - started, 2),
            }
        )
        return

    items = watcher.listing_items(source, body, final)
    for item in items:
        watcher.mark_seen(state, item)
    state["initialized_sources"].append(source.name)
    slot.update(
        {
            "status": "ok",
            "last_ok": watcher.now_iso(),
            "visible_items": len(items),
            "baseline_items": len(items),
            "last_error": None,
            "duration": round(time.time() - started, 2),
        }
    )


async def baseline_protothema(session, state, health):
    key = "lane::protothema_rss"
    if key in state["initialized_sources"]:
        return
    body, _final, status = await watcher.fetch(session, watcher.PROTOTHEMA_RSS)
    lane = health["lanes"].setdefault("protothema_rss", {})
    lane.update({"last_attempt": watcher.now_iso(), "http_status": status})
    if not body:
        lane.update({"status": "error", "last_error": f"baseline feed HTTP {status}"})
        return
    feed = feedparser.parse(body)
    count = 0
    for entry in list(feed.entries)[:80]:
        url = watcher.normalize_url(getattr(entry, "link", "") or "")
        if not url:
            continue
        item = watcher.Item(
            "ProtoThema Sports RSS",
            url,
            getattr(entry, "title", "") or "",
            getattr(entry, "published", "") or getattr(entry, "updated", "") or "",
        )
        watcher.mark_seen(state, item)
        count += 1
    state["initialized_sources"].append(key)
    lane.update(
        {
            "status": "ok",
            "last_ok": watcher.now_iso(),
            "items": count,
            "baseline_items": count,
            "last_error": None,
        }
    )


async def baseline_sportfm(session, state, health):
    key = "lane::sport_fm_tv"
    if key in state["initialized_sources"]:
        return
    lane = health["lanes"].setdefault("sport_fm_tv_keyword", {})
    total = 0
    errors = []
    for label, url in watcher.sportfm_urls():
        body, _final, status = await watcher.fetch(session, url)
        if not body:
            errors.append(f"{label}:HTTP {status}")
            continue
        feed = feedparser.parse(body)
        for entry in list(feed.entries)[:80]:
            item_url = watcher.normalize_url(getattr(entry, "link", "") or "")
            if not item_url:
                continue
            k = watcher.seen_key(item_url)
            state["keyword_seen"][k] = {"url": item_url, "at": watcher.now_iso()}
            total += 1
    if not errors or total:
        state["initialized_sources"].append(key)
    lane.update(
        {
            "status": "ok" if not errors else ("degraded" if total else "error"),
            "last_ok": watcher.now_iso() if total or not errors else None,
            "items": total,
            "baseline_items": total,
            "errors": errors,
        }
    )


async def main():
    state = watcher.load_state()
    health = {
        "started_at": watcher.now_iso(),
        "runner_alive_at": watcher.now_iso(),
        "last_cycle_finished_at": None,
        "mode": "shadow-bootstrap",
        "sources": {},
        "lanes": {},
    }
    sem = asyncio.Semaphore(watcher.CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=watcher.CONCURRENCY + 6, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(
            *(baseline_source(session, state, source, health, sem) for source in ALL_DIRECT),
            return_exceptions=True,
        )
        await baseline_protothema(session, state, health)
        await baseline_sportfm(session, state, health)

    health["runner_alive_at"] = watcher.now_iso()
    health["last_cycle_finished_at"] = watcher.now_iso()
    watcher.checkpoint(state, health)
    ok = sum(1 for v in health["sources"].values() if v.get("status") == "ok")
    failures = len(ALL_DIRECT) - len(state["initialized_sources"])
    print(
        f"SHADOW BASELINE complete direct={len(ALL_DIRECT)} ok_this_run={ok} "
        f"initialized_total={len(state['initialized_sources'])} unresolved_hint={max(0, failures)}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
