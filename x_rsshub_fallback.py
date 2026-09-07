import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests

# Public RSSHub instances. X routes on public mirrors can intermittently be
# unavailable when the instance has no working Twitter auth token.
USER_HOSTS = [
    "https://rsshub-container.folo.is",
    "https://rss.xxu.do",
    "https://rsshub.stsecurity.moe",
]
KEYWORD_HOSTS = [
    "https://rsshub-container.folo.is",
    "https://rsshub.stsecurity.moe",
    "https://rss.xxu.do",
]

# Independent read-only recovery path. These mirrors expose standard RSS and
# keep GitHub runners away from direct x.com browser access, which is currently
# returning anti-bot 403 pages. Multiple hosts are used so one dead mirror does
# not take the lane down.
NITTER_HOSTS = [
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://nitter.pek.li",
    "https://nitter.aishiteiru.moe",
    "https://nitter.aosus.link",
]

TIMEOUT = 12
USER_MAX_AGE = timedelta(days=14)

GENERAL_QUERY = (
    '"παναθηναϊκός" OR "παναθηναϊκού" OR "παναθηναϊκό" OR '
    '"παναθηναικος" OR "παναθηναικου" OR "παναθηναικο" OR '
    'from:paobc OR from:fmeetsdata'
)


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


def _status_match(value):
    value = str(value or "")
    match = re.search(
        r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([^/?#]+)/status/(\d+)",
        value,
        flags=re.I,
    )
    if match:
        return match

    # Nitter-style links keep the same /username/status/id path, but use a
    # mirror hostname rather than x.com.
    return re.search(
        r"https?://[^/]+/([^/?#]+)/status/(\d+)",
        value,
        flags=re.I,
    )


def _parse_rss(content, limit=100):
    # Some Nitter mirrors prepend BOM/whitespace before the XML declaration.
    # ElementTree rejects that form, so normalize only the transport prefix;
    # feed content, IDs, timestamps and dedupe semantics remain unchanged.
    if isinstance(content, bytes):
        content = content.lstrip(b"\xef\xbb\xbf \t\r\n")
    else:
        content = str(content or "").lstrip("\ufeff \t\r\n")
    root = ET.fromstring(content)
    found = {}
    for item in root.findall(".//item"):
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        match = _status_match(link)
        if not match:
            raw = ET.tostring(item, encoding="unicode")
            match = _status_match(raw)
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
        "User-Agent": "PAO-Watcher-X-RSS-Fallback/1.3",
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
                f"X RSS {label}: {len(tweets)} posts via {host}; "
                f"latest={tweets[0]['created'].isoformat()}",
                flush=True,
            )
            return tweets
        except Exception as exc:
            errors.append(f"{host} {type(exc).__name__}: {exc}")
    raise RuntimeError(
        f"X RSS feed failed for {label}: " + "; ".join(errors[-len(hosts):])
    )


def _fetch_nitter_user(username, limit=40):
    safe_username = quote(str(username).strip().lstrip("@"), safe="")
    return _fetch_path(
        f"/{safe_username}/rss",
        NITTER_HOSTS,
        f"Nitter user @{safe_username}",
        limit,
        max_age=USER_MAX_AGE,
    )


def _fetch_nitter_keyword(query, limit=40):
    safe_query = quote(str(query).strip(), safe="")
    return _fetch_path(
        f"/search/rss?f=tweets&q={safe_query}",
        NITTER_HOSTS,
        f"Nitter keyword {query!r}",
        limit,
    )


def fetch_user(username, limit=40):
    safe_username = quote(str(username).strip().lstrip("@"), safe="")
    try:
        return _fetch_path(
            f"/twitter/user/{safe_username}/exclude_rts_replies",
            USER_HOSTS,
            f"RSSHub user @{safe_username}",
            limit,
            max_age=USER_MAX_AGE,
        )
    except Exception as rsshub_exc:
        print(
            f"RSSHub user @{safe_username} failed: {rsshub_exc}; trying Nitter RSS",
            flush=True,
        )
        return _fetch_nitter_user(safe_username, limit)


def fetch_keyword(query, limit=40):
    safe_query = quote(str(query).strip(), safe="")
    try:
        return _fetch_path(
            f"/twitter/keyword/{safe_query}",
            KEYWORD_HOSTS,
            f"RSSHub keyword {query!r}",
            limit,
        )
    except Exception as rsshub_exc:
        print(
            f"RSSHub keyword {query!r} failed: {rsshub_exc}; trying Nitter RSS search",
            flush=True,
        )
        return _fetch_nitter_keyword(query, limit)


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
        raise RuntimeError("X RSS keyword feed returned 0 posts: " + "; ".join(errors[-4:]))
    return tweets


def fetch_general(limit=100):
    # Use the complete production query first. If RSSHub cannot serve Twitter,
    # fetch_keyword transparently moves to the independent Nitter RSS route.
    return fetch_keyword(GENERAL_QUERY, limit=limit)
