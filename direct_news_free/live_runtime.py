#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final Railway-free production runtime for PAO direct news.

This layer deliberately does NOT change the verified direct-source inventory.
It fixes production behaviour around that inventory:
- independent per-source polling so one slow publisher cannot stall the rest;
- NewsPao fast lane;
- strict real-Panathinaikos relevance (excluding Panathenaic Stadium noise);
- one atomic Telegram delivery queue with publisher-aware dedupe;
- a short chronological buffer;
- durable state checkpoint after real deliveries;
- clean SIGTERM handoff between GitHub-hosted runners.
"""

import asyncio
import hashlib
import os
import re
import signal
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp

import production_runner as production

watcher = production.watcher
runner = production.runner

BOOT_UTC = datetime.now(timezone.utc)
BOOT_MONO = time.monotonic()

NEWSPAO_SECONDS = max(3, int(os.getenv("DIRECT_NEWS_NEWSPAO_SECONDS", "5")))
CORE_SECONDS = max(6, int(os.getenv("DIRECT_NEWS_POLL_SECONDS", "12")))
INTL_PRIORITY_SECONDS = max(15, int(os.getenv("DIRECT_NEWS_INTL_PRIORITY_SECONDS", "30")))
INTL_BROAD_SECONDS = max(30, int(os.getenv("DIRECT_NEWS_INTL_BROAD_SECONDS", "60")))
PROTOTHEMA_SECONDS = max(8, int(os.getenv("DIRECT_NEWS_PROTOTHEMA_SECONDS", "15")))
SPORT_FM_TV_SECONDS = max(15, int(os.getenv("DIRECT_NEWS_SPORTFM_SECONDS", "30")))
CHECKPOINT_SECONDS = max(60, int(os.getenv("DIRECT_NEWS_CHECKPOINT_SECONDS", "120")))
ORDER_BUFFER_SECONDS = max(1.0, float(os.getenv("DIRECT_NEWS_ORDER_BUFFER_SECONDS", "2")))
CONCURRENCY = max(12, int(os.getenv("DIRECT_NEWS_CONCURRENCY", "28")))
PREBOOT_GRACE_SECONDS = max(15, int(os.getenv("DIRECT_NEWS_PREBOOT_GRACE_SECONDS", "45")))

watcher.POLL_SECONDS = CORE_SECONDS
watcher.INTL_PRIORITY_SECONDS = INTL_PRIORITY_SECONDS
watcher.INTL_BROAD_SECONDS = INTL_BROAD_SECONDS
watcher.PROTOTHEMA_SECONDS = PROTOTHEMA_SECONDS
watcher.SPORT_FM_TV_SECONDS = SPORT_FM_TV_SECONDS
watcher.CHECKPOINT_SECONDS = CHECKPOINT_SECONDS
watcher.CONCURRENCY = CONCURRENCY

_RAW_SEND_ALERT = watcher.send_alert

_STADIUM_PATTERNS = (
    re.compile(r"\bπαναθηναικ(?:ο|ου)?\s+σταδι(?:ο|ου)\b", re.I),
    re.compile(r"\bpanathenaic\s+stadium\b", re.I),
    re.compile(r"\bpanathinaiko\s+stadio\b", re.I),
)


def _clean_evidence(text):
    value = watcher.norm_text(text or "")
    for pattern in _STADIUM_PATTERNS:
        value = pattern.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def real_relevant(text):
    value = _clean_evidence(text)
    return (
        "παναθηναικ" in value
        or "panathinaik" in value
        or "panathinaikos" in value
        or "paobc" in value
    )


def real_body_hits(text):
    value = _clean_evidence(text)
    return value.count("παναθηναικ") + value.count("panathinaik") + value.count("paobc")


def strict_match_reason(source, item, body, body_ok):
    if real_relevant(getattr(item, "title", "")):
        return "title"
    if real_relevant(getattr(item, "url", "")):
        return "url"
    if not body_ok:
        return ""
    hits = real_body_hits(body)
    early_hits = real_body_hits((body or "")[:3500])
    if early_hits >= 1:
        return "body-early"
    if hits >= 2:
        return "body-strong"
    if getattr(source, "team_specific", False) and hits >= 1:
        return "team-body"
    return ""


watcher.relevant = real_relevant
watcher.body_hits = real_body_hits
watcher.match_reason = strict_match_reason
runner.strict_match_reason = strict_match_reason

_AGGREGATOR_HOSTS = {"news.google.com", "google.com", "www.google.com", "bing.com", "www.bing.com"}
_SOURCE_FAMILIES = (
    ("newspao", "newspao.gr"),
    ("sdna", "sdna.gr"),
    ("ta nea", "tanea.gr"),
    ("in.gr", "in.gr"),
    ("to10", "to10.gr"),
    ("monobala", "monobala.gr"),
    ("sportklub", "sportklub.n1info.rs"),
    ("eurohoops", "eurohoops.net"),
    ("betarades", "betarades.gr"),
    ("transferfeed", "transferfeed.com"),
    ("gazzetta", "gazzetta.gr"),
    ("sportfm", "sport-fm.gr"),
    ("sport fm", "sport-fm.gr"),
    ("novasports", "novasports.gr"),
)
_OFFICIAL_REPEAT_HOSTS = {"pao.gr", "paobc.gr", "pao1908.com"}


def _publisher_family(item):
    try:
        normalized = watcher.normalize_url(getattr(item, "url", "") or "")
        host = (urlparse(normalized).hostname or "").lower().removeprefix("www.")
        if host and host not in _AGGREGATOR_HOSTS:
            return host
    except Exception:
        pass
    source = watcher.norm_text(getattr(item, "source", "") or "")
    source = source.replace("· fallback", "").replace(" fallback", "").strip()
    for prefix, family in _SOURCE_FAMILIES:
        if source.startswith(prefix):
            return family
    return source[:120] or "unknown"


def _title_identity(title):
    value = watcher.norm_text(title or "")
    value = re.sub(r"[^a-z0-9α-ω]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def enhanced_identity_key(item):
    title = _title_identity(getattr(item, "title", "") or "")
    if len(title) < 12:
        return ""
    family = _publisher_family(item)
    published = watcher.parse_dt(getattr(item, "published", "") or "")
    day = published.strftime("%Y-%m-%d") if published else "unknown"
    if family in _OFFICIAL_REPEAT_HOSTS:
        canonical = watcher.normalize_url(getattr(item, "url", "") or "")
        raw = f"{family}\n{day}\n{canonical}\n{title}"
    else:
        raw = f"{family}\n{day}\n{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


watcher.identity_key = enhanced_identity_key

_DELIVERY_LOCK = asyncio.Lock()
_QUEUE = []
_INFLIGHT_BY_KEY = {}
_QUEUE_TASK = None
_QUEUE_SEQ = 0
_CHECKPOINT_EVENT = asyncio.Event()


def _delivery_keys(item):
    keys = set()
    try:
        url = watcher.normalize_url(getattr(item, "url", "") or "")
        if url:
            keys.add("url::" + watcher.seen_key(url))
    except Exception:
        pass
    ident = enhanced_identity_key(item)
    if ident:
        keys.add("identity::" + ident)
    return keys


def _published_sort(item, seq):
    dt = watcher.parse_dt(getattr(item, "published", "") or "")
    if dt is not None:
        return (0, dt.timestamp(), seq)
    return (1, time.time(), seq)


def _preboot_item(item):
    dt = watcher.parse_dt(getattr(item, "published", "") or "")
    if dt is None:
        return False
    return dt < BOOT_UTC - timedelta(seconds=PREBOOT_GRACE_SECONDS)


async def _queue_worker():
    global _QUEUE_TASK
    try:
        while True:
            await asyncio.sleep(ORDER_BUFFER_SECONDS)
            async with _DELIVERY_LOCK:
                if not _QUEUE:
                    _QUEUE_TASK = None
                    return
                batch = list(_QUEUE)
                _QUEUE.clear()
            batch.sort(key=lambda row: _published_sort(row[4], row[0]))
            for _seq, session, state, prefix, item, future, keys in batch:
                try:
                    await _RAW_SEND_ALERT(session, state, item, prefix=prefix)
                    watcher.mark_seen(state, item)
                    watcher.save_state(state)
                    _CHECKPOINT_EVENT.set()
                    if not future.done():
                        future.set_result(True)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                finally:
                    async with _DELIVERY_LOCK:
                        for key in keys:
                            if _INFLIGHT_BY_KEY.get(key) is future:
                                _INFLIGHT_BY_KEY.pop(key, None)
            async with _DELIVERY_LOCK:
                if not _QUEUE:
                    _QUEUE_TASK = None
                    return
    finally:
        async with _DELIVERY_LOCK:
            if _QUEUE_TASK is asyncio.current_task():
                _QUEUE_TASK = None


async def ordered_send_alert(session, state, item, prefix=""):
    global _QUEUE_TASK, _QUEUE_SEQ
    if not watcher.DELIVERY_ENABLED:
        return await _RAW_SEND_ALERT(session, state, item, prefix=prefix)
    if _preboot_item(item):
        watcher.log.info(
            "NO-REPLAY preboot skip | %s | %s | %s",
            getattr(item, "source", ""),
            getattr(item, "published", ""),
            (getattr(item, "title", "") or getattr(item, "url", ""))[:120],
        )
        watcher.mark_seen(state, item)
        watcher.save_state(state)
        return False

    keys = _delivery_keys(item)
    existing_future = None
    async with _DELIVERY_LOCK:
        try:
            if watcher.is_seen(state, getattr(item, "url", "") or ""):
                return False
        except Exception:
            pass
        ident = enhanced_identity_key(item)
        if ident and ident in state.get("identities", {}):
            return False
        for key in keys:
            future = _INFLIGHT_BY_KEY.get(key)
            if future is not None:
                existing_future = future
                break
        if existing_future is None:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            for key in keys:
                _INFLIGHT_BY_KEY[key] = future
            _QUEUE_SEQ += 1
            _QUEUE.append((_QUEUE_SEQ, session, state, prefix, item, future, keys))
            if _QUEUE_TASK is None or _QUEUE_TASK.done():
                _QUEUE_TASK = asyncio.create_task(_queue_worker(), name="telegram-order-queue")
            existing_future = future
    return await existing_future


watcher.send_alert = ordered_send_alert


def _interval_for(source):
    name = getattr(source, "name", "") or ""
    if name.startswith("NewsPao "):
        return NEWSPAO_SECONDS
    if source in watcher.INTL_PRIORITY_SOURCES:
        return INTL_PRIORITY_SECONDS
    if source in watcher.INTL_BROAD_SOURCES:
        return INTL_BROAD_SECONDS
    return CORE_SECONDS


async def _source_loop(session, state, health, source, deadline, semaphore):
    interval = _interval_for(source)
    if not (getattr(source, "name", "") or "").startswith("NewsPao "):
        initial = (int(hashlib.sha1(source.name.encode("utf-8")).hexdigest()[:4], 16) % 1500) / 1000.0
        await asyncio.sleep(initial)
    while not watcher.STOP.is_set() and time.monotonic() < deadline:
        started = time.monotonic()
        async with semaphore:
            try:
                await watcher.process_source(session, state, source, health)
            except Exception as exc:
                health["sources"].setdefault(source.name, {}).update(
                    {"status": "error", "last_error": f"{type(exc).__name__}: {exc}", "last_attempt": watcher.now_iso()}
                )
                watcher.log.warning("source failed %s: %s", source.name, exc)
        health["runner_alive_at"] = watcher.now_iso()
        health["last_cycle_finished_at"] = watcher.now_iso()
        elapsed = time.monotonic() - started
        wait = max(0.25, interval - elapsed)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(watcher.STOP.wait(), timeout=min(wait, remaining))
        except asyncio.TimeoutError:
            pass


async def _lane_loop(name, interval, func, session, state, health, deadline):
    while not watcher.STOP.is_set() and time.monotonic() < deadline:
        started = time.monotonic()
        try:
            await func(session, state, health)
        except Exception as exc:
            health["lanes"].setdefault(name, {}).update(
                {"status": "error", "last_error": f"{type(exc).__name__}: {exc}", "last_attempt": watcher.now_iso()}
            )
            watcher.log.warning("lane failed %s: %s", name, exc)
        health["runner_alive_at"] = watcher.now_iso()
        health["last_cycle_finished_at"] = watcher.now_iso()
        elapsed = time.monotonic() - started
        wait = max(0.25, interval - elapsed)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(watcher.STOP.wait(), timeout=min(wait, remaining))
        except asyncio.TimeoutError:
            pass


async def _checkpoint_loop(state, health, deadline):
    while not watcher.STOP.is_set() and time.monotonic() < deadline:
        timeout = min(CHECKPOINT_SECONDS, max(0.5, deadline - time.monotonic()))
        triggered = False
        try:
            await asyncio.wait_for(_CHECKPOINT_EVENT.wait(), timeout=timeout)
            triggered = True
        except asyncio.TimeoutError:
            pass
        if triggered:
            _CHECKPOINT_EVENT.clear()
            await asyncio.sleep(2)
        health["runner_alive_at"] = watcher.now_iso()
        try:
            watcher.checkpoint(state, health)
        except Exception as exc:
            watcher.log.warning("checkpoint loop failed: %s", exc)


async def live_main():
    state = watcher.load_state()
    health = {
        "started_at": watcher.now_iso(),
        "runner_alive_at": watcher.now_iso(),
        "last_cycle_finished_at": None,
        "mode": "production" if watcher.DELIVERY_ENABLED else "shadow",
        "runtime": "live_runtime_v1",
        "sources": {},
        "lanes": {},
    }
    if watcher.DELIVERY_ENABLED and not watcher.TOKEN:
        raise SystemExit("Delivery enabled but TELEGRAM_BOT_TOKEN_V2 is missing")

    start = time.monotonic()
    deadline = start + watcher.MAX_RUNTIME_SECONDS
    semaphore = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY + 12, ttl_dns_cache=300)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, watcher.STOP.set)
        except (NotImplementedError, RuntimeError):
            pass

    all_sources = list(watcher.CORE_SOURCES) + list(watcher.RESTORED_SOURCES) + list(watcher.INTL_PRIORITY_SOURCES) + list(watcher.INTL_BROAD_SOURCES)
    if len(all_sources) != 126:
        raise SystemExit(f"Direct source inventory changed unexpectedly: {len(all_sources)} != 126")

    watcher.log.info(
        "LIVE runtime ready | sources=%d newspao=%ss core=%ss intlP=%ss intlB=%ss concurrency=%d order_buffer=%.1fs",
        len(all_sources), NEWSPAO_SECONDS, CORE_SECONDS, INTL_PRIORITY_SECONDS, INTL_BROAD_SECONDS, CONCURRENCY, ORDER_BUFFER_SECONDS,
    )

    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            if watcher.DELIVERY_ENABLED and not await watcher.discover_recipients(session, state):
                raise SystemExit("Delivery enabled but Telegram recipients are unavailable")
            tasks = [
                asyncio.create_task(_source_loop(session, state, health, source, deadline, semaphore), name=f"direct:{source.name}")
                for source in all_sources
            ]
            tasks.extend(
                [
                    asyncio.create_task(_lane_loop("protothema_rss", PROTOTHEMA_SECONDS, watcher.protothema_cycle, session, state, health, deadline), name="lane:protothema"),
                    asyncio.create_task(_lane_loop("sport_fm_tv_keyword", SPORT_FM_TV_SECONDS, watcher.sportfm_cycle, session, state, health, deadline), name="lane:sportfm"),
                    asyncio.create_task(_checkpoint_loop(state, health, deadline), name="state:checkpoint"),
                ]
            )
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        queue_task = _QUEUE_TASK
        if queue_task is not None and not queue_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(queue_task), timeout=ORDER_BUFFER_SECONDS + 20)
            except Exception:
                pass
        try:
            watcher.checkpoint(state, health)
        except Exception as exc:
            watcher.log.warning("final checkpoint failed: %s", exc)
        watcher.log.info("clean GitHub runner handoff after %.1f minutes", (time.monotonic() - start) / 60)


watcher.main = live_main


if __name__ == "__main__":
    asyncio.run(production.main())
