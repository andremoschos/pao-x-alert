#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small source additions layered on top of the verified direct-news inventory."""


def apply(watcher):
    """Add extra direct sources idempotently before bootstrap/preflight/runtime."""
    extras = [
        watcher.Source("Sportime", "https://www.sportime.gr/"),
    ]

    existing = {(source.name, source.url) for source in watcher.CORE_SOURCES}
    added = 0
    for source in extras:
        key = (source.name, source.url)
        if key not in existing:
            watcher.CORE_SOURCES.append(source)
            existing.add(key)
            added += 1
    return added
