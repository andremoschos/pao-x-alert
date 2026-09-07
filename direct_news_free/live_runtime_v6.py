#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Content-only hardening for PAO Direct News Free.

Keeps v5's 126-source inventory, relevance, no-replay, ordering, global atomic
Telegram dedupe and single-sender behaviour.  Adds a final global exclusion for
non-editorial sports resources: match/live-score/result/fixture/schedule/
standings pages.  These are useful links on team pages but are not news
articles and must never reach Telegram.
"""

import asyncio
import re
from urllib.parse import unquote, urlparse

import live_runtime_v5 as stable

base = stable.base
watcher = base.watcher
article_only = stable.article_only

_PREVIOUS_OBVIOUS_LANDING = article_only.obvious_landing
_PREVIOUS_RAW_SEND = base._RAW_SEND_ALERT

# URL path markers that identify scorecards, fixtures, schedules, standings or
# live-match resources rather than editorial articles.  Match by path segment /
# distinctive fragment, not arbitrary title words, to avoid suppressing a real
# article that merely mentions a result.
_NON_ARTICLE_PATH_MARKERS = (
    "/match-direct/",
    "/matchs-direct/",
    "/live-score/",
    "/livescore/",
    "/live-scores/",
    "/scores/",
    "/fixtures/",
    "/fixture/",
    "/standings/",
    "/standing/",
    "/classement/",
    "/classements/",
    "/calendrier/",
    "/calendar/",
    "/schedule/",
    "/schedules/",
    "/resultats/",
    "/results/",
    "/page-calendrier",
    "/page-classement",
)

# Generic result/live page titles.  watcher.norm_text removes accents, so the
# patterns below deliberately use normalized forms such as "termine".
_NON_ARTICLE_TITLE_PATTERNS = (
    re.compile(r"\bmatch\s+termine\b", re.I),
    re.compile(r"\bmatch\s+en\s+direct\b", re.I),
    re.compile(r"\blive\s+score\b", re.I),
    re.compile(r"\bfull\s+time\b", re.I),
    re.compile(r"\bfinal\s+score\b", re.I),
    re.compile(r"\bcalendrier\s+et\s+resultats\b", re.I),
    re.compile(r"\bfixtures?\s+(?:and|&)\s+results?\b", re.I),
    re.compile(r"\bstandings\b", re.I),
    re.compile(r"\bclassement\b", re.I),
)


def _normalized_path(url):
    try:
        return unquote((urlparse(watcher.normalize_url(url or "")).path or "").lower())
    except Exception:
        return ""


def non_editorial_resource(url="", title=""):
    path = _normalized_path(url)
    if path and any(marker in path for marker in _NON_ARTICLE_PATH_MARKERS):
        return True

    normalized_title = watcher.norm_text(title or "")
    if normalized_title and any(pattern.search(normalized_title) for pattern in _NON_ARTICLE_TITLE_PATTERNS):
        return True

    return False


def content_only_landing(url, title=""):
    if non_editorial_resource(url, title):
        return True
    return _PREVIOUS_OBVIOUS_LANDING(url, title)


# v4's article_candidate, match_reason and raw-send wrapper resolve
# article_only.obvious_landing dynamically, so replacing this one global guard
# applies the rule at discovery, hydration/relevance and send time.
article_only.obvious_landing = content_only_landing


async def content_only_raw_send(session, state, item, prefix=""):
    if non_editorial_resource(getattr(item, "url", ""), getattr(item, "title", "")):
        watcher.log.info(
            "NON-ARTICLE SPORTS PAGE SUPPRESSED | %s | %s | %s",
            getattr(item, "source", ""),
            getattr(item, "title", ""),
            getattr(item, "url", ""),
        )
        return False
    return await _PREVIOUS_RAW_SEND(session, state, item, prefix=prefix)


# Final gate immediately before v5's atomic reservation / Telegram send.
base._RAW_SEND_ALERT = content_only_raw_send


def _selftest():
    # Exact class of false positives reported from L'Equipe.
    assert non_editorial_resource(
        "https://www.lequipe.fr/Football/match-direct/ligue-conference/2026-2027/panathinaikos-cska-1948-sofia-live/698240",
        "Panathinaïkos 1-1 CSKA 1948 Sofia, Ligue Conference : match terminé",
    )
    assert non_editorial_resource(
        "https://www.lequipe.fr/Football/match-direct/ligue-conference/2026-2027/paks-panathinaikos-live/695976",
        "Paks 1-2 Panathinaïkos, Ligue Conference : match terminé",
    )
    assert non_editorial_resource(
        "https://www.lequipe.fr/Football/ligue-conference/page-calendrier-general/panathinaikos",
        "Panathinaïkos - Calendrier et résultats Ligue Conférence 2026-2027",
    )

    # Real editorial articles remain allowed by this extra guard.
    assert not non_editorial_resource(
        "https://newspao.gr/podosfairo/774592_taseis-paramonis-apo-nte-frai-anypomono-na-paixo-sto-votaniko.html",
        "Τάσεις παραμονής από Ντε Φράι: Ανυπομονώ να παίξω στον Βοτανικό",
    )
    assert not non_editorial_resource(
        "https://www.sportal.gr/podosfairo/article/nte-frai-panathinaikos-opos-inter-123456",
        "Ντε Φράι: Παναθηναϊκός όπως Ίντερ, ανυπομονώ να αγωνιστώ στον Βοτανικό",
    )


if __name__ == "__main__":
    _selftest()
    stable._selftest()
    asyncio.run(base.production.main())
