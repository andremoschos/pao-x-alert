import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests

# Public RSSHub instances verified from GitHub-hosted runners on 2026-09-06.
# Direct X remains primary; these are read-only fallbacks when X blocks the runner.
# folo currently carries fresh PAO user + keyword feeds. stsecurity is kept only
# as a secondary user-feed fallback and is rejected when its mirror is stale.
USER_HOSTS = [
    "https://rsshub-container.folo.is",
    "https://rsshub.stsecurity.moe",
]
KEYWORD_HOSTS = [
    "https://rsshub-container.folo.is",
    "https://rsshub.stsecurity.moe",
]
TIMEOUT = 12
USER_MAX_AGE = timedelta(days=14)


def snowflake_datetime(tweet_id):
    ms = (int(tweet_id) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _strip_html(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split()).strip()


def _item_text(item):
    title = item.findtext("title") or ""
    description = item.findtext("description") or ""
    text = _strip_html(title)
    if not text:
        text = _strip_html(description)
    return text or "(post without text)"


def _parse_rss(content, limit=100):
    root = ET.fromstring(content)
    found = {}
    for item in root.findall(".//item"):
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        match = re.search(
            r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([^/?#]+)/status/(\d+)",
            link,
            flags=re.I,
        )
        if not match:
            # Some RSSHub descriptions contain the status link instead of <link>.
            raw = ET.tostring(item, encoding="unicode")
            match = re.search(
                r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([^/?#]+)/status/(\d+)",
                raw,
                flags=re.I,
            )
        if not match:
            continue
        username, tid = match.group(1), match.group(2)
        found[tid] = {
            "id": tid,
            "author": f"@{username}",
            "text": _item_text(item),
            "url": f"https://x.com/{username}/status/{tid}",
            "created": snowflake_datetime(tid),
            "media": [],
        }
        if len(found) >= limit:
            break
    return sorted(found.values(), key=lambda item: int(item["id"]), reverse=True)


def _fetch_path(path, hosts, label, limit, max_age=None):
    errors = []
    headers = {
        "User-Agent": "PAO-Watcher-X-RSS-Fallback/1.1",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    for host in hosts:
        url = host.rstrip("/") + path
        try:
            response = requests.get(url, timeout=TIMEOUT, headers=headers)
            if response.status_code != 200:
                errors.append(f"{host} HTTP {response.status_code}")
                continue
            tweets = _parse_rss(response.content, limit=limit)
            if not tweets:
                errors.append(f"{host} empty feed")
                continue
            if max_age is not None:
                age = datetime.now(timezone.utc) - tweets[0]["created"]
                if age > max_age:
                    errors.append(
                        f"{host} stale feed latest={tweets[0]['created'].isoformat()}"
                    )
                    continue
            print(
                f"X RSSHub fallback {label}: {len(tweets)} posts via {host}; "
                f"latest={tweets[0]['created'].isoformat()}",
                flush=True,
            )
            return tweets
        except Exception as exc:
            errors.append(f"{host} {type(exc).__name__}: {exc}")
    raise RuntimeError(
        f"RSSHub X fallback failed for {label}: " + "; ".join(errors[-len(hosts):])
    )


def fetch_user(username, limit=40):
    safe_username = quote(str(username).strip().lstrip("@"), safe="")
    return _fetch_path(
        f"/twitter/user/{safe_username}/exclude_rts_replies",
        USER_HOSTS,
        f"user @{safe_username}",
        limit,
        max_age=USER_MAX_AGE,
    )


def fetch_keyword(query, limit=40):
    safe_query = quote(str(query).strip(), safe="")
    return _fetch_path(
        f"/twitter/keyword/{safe_query}",
        KEYWORD_HOSTS,
        f"keyword {query!r}",
        limit,
    )


def fetch_many_keywords(queries, limit=100):
    unique_queries = []
    for query in queries:
        query = str(query or "").strip()
        if query and query not in unique_queries:
            unique_queries.append(query)

    found = {}
    errors = []
    workers = min(4, max(1, len(unique_queries)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_keyword, query, min(limit, 40)): query for query in unique_queries}
        for future in as_completed(futures):
            query = futures[future]
            try:
                for tweet in future.result():
                    found[tweet["id"]] = tweet
            except Exception as exc:
                errors.append(f"{query!r}: {exc}")

    tweets = sorted(found.values(), key=lambda item: int(item["id"]), reverse=True)[:limit]
    if not tweets:
        raise RuntimeError("RSSHub keyword fallback returned 0 posts: " + "; ".join(errors[-4:]))
    return tweets


def fetch_general(limit=100):
    # Equivalent coverage to the primary Greek PAO query, using smaller feeds
    # because public RSSHub keyword endpoints are more reliable with compact terms.
    queries = [
        "Παναθηναϊκός",
        "Παναθηναϊκού",
        "Παναθηναϊκό",
        "παναθηναικος",
    ]
    found = {tweet["id"]: tweet for tweet in fetch_many_keywords(queries, limit=limit)}

    # Preserve the two account-specific clauses from the primary query when available.
    for username in ("paobc", "fmeetsdata"):
        try:
            for tweet in fetch_user(username, limit=40):
                found[tweet["id"]] = tweet
        except Exception as exc:
            print(f"X RSSHub optional @{username} feed unavailable: {exc}", flush=True)

    return sorted(found.values(), key=lambda item: int(item["id"]), reverse=True)[:limit]
