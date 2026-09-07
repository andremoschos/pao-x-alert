#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final cross-run duplicate protection for PAO Direct News Free.

This layer keeps v4 source inventory, relevance, article-only filtering,
ordering, no-replay and single-sender behaviour unchanged.  It adds one last
atomic reservation immediately before the Telegram API call.

Why this exists: local state/in-memory locks cannot prove exclusivity across two
GitHub-hosted processes during a restart/handoff.  The delivery ledger lives in
GitHub itself and is updated with the Contents API using SHA compare-and-swap.
If two routes/runners race on the same article, only one reservation can win.
Publication timestamps are deliberately NOT part of the duplicate identity.
"""

import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import live_runtime_v4 as article_only

base = article_only.base
watcher = base.watcher

_PREVIOUS_RAW_SEND = base._RAW_SEND_ALERT
_GH_TOKEN = (os.getenv("DIRECT_NEWS_GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
_GH_REPO = os.getenv("GITHUB_REPOSITORY", "andremoschos/pao-x-alert").strip() or "andremoschos/pao-x-alert"
_LEDGER_PATH = "direct_news_free/delivery_ledger.json"
_LEDGER_API = f"https://api.github.com/repos/{_GH_REPO}/contents/{_LEDGER_PATH}"
_LEDGER_RETENTION_HOURS = 72
_LEDGER_MAX_KEYS = 12000
_LEDGER_RETRIES = 8
_LOCAL_LEDGER_LOCK = asyncio.Lock()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value):
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def delivery_keys(item):
    """Return timestamp-independent keys for the same publisher article."""
    keys = set()

    try:
        normalized = watcher.normalize_url(getattr(item, "url", "") or "")
    except Exception:
        normalized = ""
    if normalized:
        url_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        keys.add("url::" + url_hash)

    try:
        ident = watcher.identity_key(item)
    except Exception:
        ident = ""
    if ident:
        keys.add("identity::" + ident)

    # Independent fallback, deliberately ignoring timestamp and discovery path.
    try:
        family = base._publisher_family(item)
        title = base._title_identity(getattr(item, "title", "") or "")
    except Exception:
        family, title = "", ""
    if family and len(title) >= 12:
        raw = f"{family}\n{title}"
        keys.add("publisher-title::" + hashlib.sha256(raw.encode("utf-8")).hexdigest())

    return keys


def _prune(entries):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_LEDGER_RETENTION_HOURS)
    kept = {}
    sortable = []
    for key, meta in (entries or {}).items():
        if not isinstance(meta, dict):
            continue
        dt = _parse_iso(meta.get("at"))
        if dt is None or dt < cutoff:
            continue
        kept[str(key)] = meta
        sortable.append((dt.timestamp(), str(key)))

    if len(kept) > _LEDGER_MAX_KEYS:
        sortable.sort(reverse=True)
        allowed = {key for _ts, key in sortable[:_LEDGER_MAX_KEYS]}
        kept = {key: value for key, value in kept.items() if key in allowed}
    return kept


def _headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {_GH_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "User-Agent": "PAODirectNewsAtomicLedger/1.0",
    }


async def _read_ledger(session):
    async with session.get(
        _LEDGER_API,
        params={"ref": "main", "v": str(datetime.now(timezone.utc).timestamp())},
        headers=_headers(),
        timeout=10,
    ) as response:
        if response.status == 404:
            return {"version": 1, "entries": {}}, None
        if response.status != 200:
            text = (await response.text())[:300]
            raise RuntimeError(f"delivery ledger GET HTTP {response.status}: {text}")
        payload = await response.json()
        sha = str(payload.get("sha") or "").strip() or None
        raw = base64.b64decode(payload.get("content") or b"").decode("utf-8")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            data = {}
        data.setdefault("version", 1)
        data["entries"] = _prune(data.get("entries") or {})
        return data, sha


async def _write_ledger(session, data, sha):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    payload = {
        "message": "Reserve Telegram article delivery [skip ci]",
        "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha

    async with session.put(
        _LEDGER_API,
        json=payload,
        headers=_headers(),
        timeout=12,
    ) as response:
        if response.status in (200, 201):
            return True
        if response.status in (409, 422):
            # Another runner/article updated the ledger after our GET.
            return False
        text = (await response.text())[:300]
        raise RuntimeError(f"delivery ledger PUT HTTP {response.status}: {text}")


async def reserve_delivery(session, item):
    """Atomically reserve all article keys. False means already delivered."""
    keys = delivery_keys(item)
    if not keys:
        raise RuntimeError("cannot build a stable delivery key")
    if not _GH_TOKEN:
        raise RuntimeError("DIRECT_NEWS_GITHUB_TOKEN missing; refusing unguarded Telegram send")

    # One process serializes its own writers; GitHub SHA CAS serializes all
    # processes/runners.  This gives us one global send decision.
    async with _LOCAL_LEDGER_LOCK:
        for attempt in range(1, _LEDGER_RETRIES + 1):
            data, sha = await _read_ledger(session)
            entries = data.get("entries") or {}
            if any(key in entries for key in keys):
                return False

            meta = {
                "at": _now_iso(),
                "source": str(getattr(item, "source", "") or "")[:120],
                "url": str(getattr(item, "url", "") or "")[:700],
            }
            for key in keys:
                entries[key] = meta
            data["entries"] = _prune(entries)
            data["updated_at"] = _now_iso()

            if await _write_ledger(session, data, sha):
                return True

            # CAS conflict: refetch.  If the conflicting update was the same
            # article the next iteration returns False; otherwise we merge it.
            await asyncio.sleep(min(0.15 * attempt, 1.0))

    raise RuntimeError("delivery ledger CAS contention did not settle")


async def globally_deduped_raw_send(session, state, item, prefix=""):
    reserved = await reserve_delivery(session, item)
    if not reserved:
        watcher.log.info(
            "GLOBAL DUPLICATE SUPPRESSED | %s | %s | %s",
            getattr(item, "source", ""),
            getattr(item, "title", ""),
            getattr(item, "url", ""),
        )
        return False

    try:
        return await _PREVIOUS_RAW_SEND(session, state, item, prefix=prefix)
    except Exception:
        # Reservation is intentionally retained. Telegram send can succeed and
        # the client can still lose the HTTP response; deleting the reservation
        # here would reintroduce duplicate delivery on retry.
        raise


# This is the final gate reached by live_runtime's single chronological queue.
base._RAW_SEND_ALERT = globally_deduped_raw_send


def _selftest():
    title = "Ντε Φράι: «Παναθηναϊκός όπως Ίντερ, ανυπομονώ να αγωνιστώ στον Βοτανικό!»"
    url = "https://www.sportal.gr/podosfairo/article/nte-frai-panathinaikos-opos-inter-123456"
    a = watcher.Item("Sportal", url, title, "07/09 19:25")
    b = watcher.Item("Sportal", url, title, "2026-09-07T16:25:00+00:00")
    assert delivery_keys(a) == delivery_keys(b), (delivery_keys(a), delivery_keys(b))
    assert any(key.startswith("publisher-title::") for key in delivery_keys(a))


if __name__ == "__main__":
    _selftest()
    asyncio.run(base.production.main())
