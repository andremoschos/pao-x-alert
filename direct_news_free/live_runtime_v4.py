#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Article-only hardening for PAO Direct News Free.

Keeps the verified direct-source inventory and v3 behaviour intact while
ensuring Telegram receives actual article/post pages only, never category,
team, tag, latest, archive, search or other landing pages.
"""

import asyncio
import re
from urllib.parse import parse_qs, unquote, urlparse

import live_runtime_v3 as final

base = final.base
watcher = base.watcher

_ORIGINAL_ARTICLEISH = watcher.articleish
_ORIGINAL_MATCH_REASON = watcher.match_reason
_PREVIOUS_RAW_SEND = base._RAW_SEND_ALERT


def _all_sources():
    return (
        list(watcher.CORE_SOURCES)
        + list(watcher.RESTORED_SOURCES)
        + list(watcher.INTL_PRIORITY_SOURCES)
        + list(watcher.INTL_BROAD_SOURCES)
    )


_SOURCE_LANDINGS = {
    watcher.normalize_url(source.url)
    for source in _all_sources()
    if getattr(source, "url", "")
}

_ALWAYS_LANDING_FIRST = {
    "tag", "tags", "category", "categories", "author", "authors",
    "search", "topics", "topic", "sections", "section",
}

_TEAM_LANDING_FIRST = {"team", "teams", "club", "clubs"}

_GENERIC_LANDING_SLUGS = {
    "latest", "latest-news", "all-news", "newsroom", "archive", "archives",
    "roi-eidiseon", "roi", "pao-nea", "sports", "sport", "football",
    "basket", "basketball", "podosfairo", "mpasket", "volei", "erasitexnis",
    "athlitika", "athlitikanea", "blog-view", "results", "resultados",
}

_GENERIC_TITLES = {
    "latest news", "all news", "news", "sports", "sport", "football",
    "basketball", "basket", "panathinaikos", "παναθηναικος", "παναθηναικοσ",
    "olympiakos", "ολυμπιακος", "ολυμπιακοσ", "ποδοσφαιρο", "μπασκετ",
    "αθλητικα", "ροη ειδησεων", "τελευταια νεα", "νεα",
}


def _url_parts(url):
    try:
        parsed = urlparse(watcher.normalize_url(url or ""))
    except Exception:
        return None, [], {}
    path = unquote(parsed.path or "").strip("/").lower()
    parts = [part for part in path.split("/") if part]
    return parsed, parts, parse_qs(parsed.query or "")


def _strong_article_signal(url):
    parsed, parts, query = _url_parts(url)
    if parsed is None or not parts:
        return False

    path = "/" + "/".join(parts)
    if path.startswith("/transfers/"):
        return True
    if re.search(r"/20\d{2}/\d{1,2}(?:/\d{1,2})?/", path + "/"):
        return True
    if any(re.fullmatch(r"\d{4,}", part) for part in parts):
        return True
    for key in ("p", "id", "article", "article_id", "post", "post_id"):
        for value in query.get(key, []):
            if str(value).isdigit() and len(str(value)) >= 3:
                return True

    tail = parts[-1]
    if tail.endswith((".html", ".htm")) and len(tail) >= 16:
        return True
    if "-" in tail and len(tail) >= 20 and tail.count("-") >= 2:
        return True
    return False


def _generic_title(title):
    value = watcher.norm_text(title or "").strip()
    if not value:
        return True
    value = re.sub(r"\s+", " ", value)
    if value in _GENERIC_TITLES:
        return True
    # Navigation/category labels are often one or two bare words.  Do not block
    # a genuine short headline when its URL has a strong article signature.
    words = re.findall(r"[a-z0-9α-ω]+", value)
    return len(words) <= 2 and not any(ch in value for ch in (":", "-", "«", "»", "?", "!"))


def obvious_landing(url, title=""):
    normalized = watcher.normalize_url(url or "")
    if not normalized:
        return True
    if normalized in _SOURCE_LANDINGS:
        return True

    parsed, parts, _query = _url_parts(normalized)
    if parsed is None or not parts:
        return True

    strong = _strong_article_signal(normalized)
    first = parts[0]
    tail = parts[-1]

    if first in _ALWAYS_LANDING_FIRST and not strong:
        return True
    if first in _TEAM_LANDING_FIRST:
        # /teams/panathinaikos and /teams/olympiakos are landing pages.  A rare
        # deeper team URL is allowed only if it carries an unmistakable article
        # id/date/descriptive slug.
        if len(parts) <= 2 or not strong:
            return True
    if tail in _GENERIC_LANDING_SLUGS and not strong:
        return True

    # Bare one-segment navigation pages such as /olympiakos, /football,
    # /panathinaikos are not articles unless the URL itself has an article id.
    if len(parts) == 1 and not strong:
        return True

    if _generic_title(title) and not strong:
        return True
    return False


def article_candidate(url, anchor=""):
    if obvious_landing(url, anchor):
        return False
    return _ORIGINAL_ARTICLEISH(url, anchor)


def article_only_match_reason(source, item, body, body_ok):
    if obvious_landing(getattr(item, "url", ""), getattr(item, "title", "")):
        return ""

    reason = _ORIGINAL_MATCH_REASON(source, item, body, body_ok)
    if not reason:
        return ""

    # At this stage the candidate has been fetched.  Strong URL shape or a real
    # publication timestamp is enough.  Otherwise require substantial page text
    # and a non-navigation title, avoiding category grids that happened to
    # contain Panathinaikos in cards/sidebar content.
    if _strong_article_signal(getattr(item, "url", "")):
        return reason
    if watcher.parse_dt(getattr(item, "published", "") or "") is not None:
        return reason
    if body_ok and len((body or "").strip()) >= 500 and not _generic_title(getattr(item, "title", "")):
        return reason
    return ""


async def article_only_raw_send(session, state, item, prefix=""):
    if obvious_landing(getattr(item, "url", ""), getattr(item, "title", "")):
        watcher.log.info(
            "LANDING SUPPRESSED | %s | %s | %s",
            getattr(item, "source", ""),
            getattr(item, "title", ""),
            getattr(item, "url", ""),
        )
        return False
    return await _PREVIOUS_RAW_SEND(session, state, item, prefix=prefix)


watcher.articleish = article_candidate
watcher.match_reason = article_only_match_reason
base.runner.strict_match_reason = article_only_match_reason
base._RAW_SEND_ALERT = article_only_raw_send


def _selftest():
    # User-reported false positives.
    assert obvious_landing("https://www.gazzetta.gr/teams/olympiakos", "ΟΛΥΜΠΙΑΚΟΣ")
    assert obvious_landing("https://www.pao.gr/all-news/", "LATEST NEWS")
    assert obvious_landing("https://www.gazzetta.gr/teams/panathinaikos", "ΠΑΝΑΘΗΝΑΪΚΟΣ")
    # Real article patterns remain valid.
    assert not obvious_landing(
        "https://newspao.gr/podosfairo/774592_taseis-paramonis-apo-nte-frai-anypomono-na-paixo-sto-votaniko.html",
        "Τάσεις παραμονής από Ντε Φράι: Ανυπομονώ να παίξω στον Βοτανικό",
    )
    assert not obvious_landing(
        "https://www.transferfeed.com/transfers/example-panathinaikos-player/123456",
        "Player transfer from Panathinaikos",
    )


if __name__ == "__main__":
    _selftest()
    asyncio.run(base.production.main())
