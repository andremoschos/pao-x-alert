#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final direct-news hardening layer.

Keeps the verified source inventory intact and fixes four production issues:
1) one active Telegram sender across GitHub runner handoffs (lease guard),
2) source-independent duplicate suppression inherited from v2,
3) complete Panathinaikos keyword coverage including exact PAO/ΠΑΟ tokens,
4) TransferFeed relative-age parsing so stale transfers never pass as undated.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import live_runtime_v2 as stable

base = stable.base
watcher = base.watcher

RUN_ID = str(os.getenv("DIRECT_NEWS_RUN_ID") or os.getenv("GITHUB_RUN_ID") or "").strip()
LEASE_URL = "https://raw.githubusercontent.com/andremoschos/pao-x-alert/main/direct_news_free/active_run.json"
_LEASE_CACHE_AT = 0.0
_LEASE_CACHE_RUN = None
_LEASE_CACHE_OK = True
_LEASE_CACHE_SECONDS = 1.0
_RAW_TELEGRAM_SEND = base._RAW_SEND_ALERT


def complete_relevant(text):
    value = base._clean_evidence(text or "")
    if not value:
        return False
    return (
        "παναθηναικ" in value
        or "panathinaik" in value
        or "paobc" in value
        or bool(re.search(r"\bπαο\b", value))
        or bool(re.search(r"\bpao\b", value))
    )


def complete_body_hits(text):
    value = base._clean_evidence(text or "")
    if not value:
        return 0
    return (
        len(re.findall(r"\bπαναθηναικ[α-ω]*\b", value))
        + len(re.findall(r"\bpanathinaik[a-z]*\b", value))
        + len(re.findall(r"\bpaobc\b", value))
        + len(re.findall(r"\bπαο\b", value))
        + len(re.findall(r"\bpao\b", value))
    )


# live_runtime.strict_match_reason resolves these globals at call time.
base.real_relevant = complete_relevant
base.real_body_hits = complete_body_hits
watcher.relevant = complete_relevant
watcher.body_hits = complete_body_hits
watcher.match_reason = base.strict_match_reason
base.runner.strict_match_reason = base.strict_match_reason


_RELATIVE_AGE = re.compile(
    r"(?<!\w)(\d{1,4})\s*(mo|months?|w|weeks?|d|days?|h|hrs?|hours?|m|mins?|minutes?|s|secs?|seconds?|y|yrs?|years?)\s*ago\b",
    re.I,
)


def _relative_age_delta(text):
    match = _RELATIVE_AGE.search(str(text or ""))
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit in {"s", "sec", "secs", "second", "seconds"}:
        return timedelta(seconds=amount)
    if unit in {"m", "min", "mins", "minute", "minutes"}:
        return timedelta(minutes=amount)
    if unit in {"h", "hr", "hrs", "hour", "hours"}:
        return timedelta(hours=amount)
    if unit in {"d", "day", "days"}:
        return timedelta(days=amount)
    if unit in {"w", "week", "weeks"}:
        return timedelta(weeks=amount)
    if unit in {"mo", "month", "months"}:
        return timedelta(days=30 * amount)
    if unit in {"y", "yr", "yrs", "year", "years"}:
        return timedelta(days=365 * amount)
    return None


_ORIGINAL_TRANSFERFEED_ITEMS = watcher.transferfeed_items


def transferfeed_items_with_age(source, body, final):
    items = _ORIGINAL_TRANSFERFEED_ITEMS(source, body, final)
    now = datetime.now(timezone.utc)
    for item in items:
        evidence = " ".join(
            str(v or "")
            for v in (
                getattr(item, "title", ""),
                getattr(item, "context", ""),
            )
        )
        delta = _relative_age_delta(evidence)
        if delta is not None:
            item.published = (now - delta).isoformat()
    return items


watcher.transferfeed_items = transferfeed_items_with_age


async def _lease_is_current(session):
    """Fail open on transient GitHub read errors, fail closed for a known newer run."""
    global _LEASE_CACHE_AT, _LEASE_CACHE_RUN, _LEASE_CACHE_OK
    if not RUN_ID:
        return True

    now_mono = time.monotonic()
    if now_mono - _LEASE_CACHE_AT < _LEASE_CACHE_SECONDS:
        return _LEASE_CACHE_OK

    try:
        cache_bust = int(time.time() * 1000)
        async with session.get(
            f"{LEASE_URL}?v={cache_bust}",
            headers={"Cache-Control": "no-cache, no-store, max-age=0"},
            timeout=5,
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"lease HTTP {response.status}")
            payload = json.loads(await response.text())
            remote_run = str(payload.get("run_id") or "").strip()
            _LEASE_CACHE_RUN = remote_run
            _LEASE_CACHE_OK = (not remote_run) or remote_run == RUN_ID
            _LEASE_CACHE_AT = now_mono
            return _LEASE_CACHE_OK
    except Exception as exc:
        # Do not lose genuine news because GitHub raw had a transient failure.
        watcher.log.warning("sender lease check failed open: %s", exc)
        _LEASE_CACHE_AT = now_mono
        return True


async def guarded_raw_send(session, state, item, prefix=""):
    # GitHub sends SIGTERM to the old runner during replacement.  Never drain a
    # pending Telegram queue after STOP: that is the main handoff duplicate race.
    if watcher.STOP.is_set():
        watcher.log.info("SEND SUPPRESSED after STOP | %s", getattr(item, "source", ""))
        return False

    if not await _lease_is_current(session):
        watcher.log.info(
            "SEND SUPPRESSED stale runner | local=%s remote=%s | %s",
            RUN_ID,
            _LEASE_CACHE_RUN,
            getattr(item, "source", ""),
        )
        watcher.STOP.set()
        return False

    return await _RAW_TELEGRAM_SEND(session, state, item, prefix=prefix)


# live_runtime queue resolves this global at send time.
base._RAW_SEND_ALERT = guarded_raw_send


if __name__ == "__main__":
    asyncio.run(base.production.main())
