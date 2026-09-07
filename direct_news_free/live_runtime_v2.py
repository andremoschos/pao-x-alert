#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stable cross-path dedupe layer for the Railway-free PAO direct-news runtime.

Imports live_runtime_v1 and tightens only article identity.  The verified 126
source inventory, polling, relevance rules, chronological queue and Telegram
routing remain unchanged.

Root cause fixed here: the previous identity included the parsed publication
day.  The same publisher article can arrive once from a direct page with an ISO
timestamp and again from another direct/fallback path with a local, RFC or
missing timestamp.  That produced different hashes for the same headline and
let a duplicate through.  Normal publishers are now identified by publisher +
normalized headline, independent of timestamp and discovery path.  Official PAO
publishers that can intentionally reuse a headline also include canonical URL.
"""

import asyncio
import hashlib

import live_runtime as base

watcher = base.watcher


def stable_identity_key(item):
    title = base._title_identity(getattr(item, "title", "") or "")
    if len(title) < 12:
        return ""

    family = base._publisher_family(item)
    if family in base._OFFICIAL_REPEAT_HOSTS:
        canonical = watcher.normalize_url(getattr(item, "url", "") or "")
        raw = f"{family}\n{canonical}\n{title}"
    else:
        # Deliberately no publication timestamp here.  Timestamp formatting is
        # metadata and must never make one publisher article look like two.
        raw = f"{family}\n{title}"

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# live_runtime's queue and delivery guard resolve this global at call time, so
# replacing it here hardens URL/direct/fallback sibling paths without rebuilding
# the runtime or changing any source definitions.
base.enhanced_identity_key = stable_identity_key
watcher.identity_key = stable_identity_key


if __name__ == "__main__":
    asyncio.run(base.production.main())
