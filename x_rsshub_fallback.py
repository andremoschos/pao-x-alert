import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests

FX_BASE = "https://api.fxtwitter.com/2"
FX_TIMEOUT = 15

USER_HOSTS = [
    "https://rss.xxu.do",
    "https://rsshub.stsecurity.moe",
]
KEYWORD_HOSTS = [
    "https://rsshub.stsecurity.moe",
    "https://rss.xxu.do",
]

# Nitter is now only a last-resort safety net. Public instances are frequently
# empty/rate-limited, so do not spend a long time cycling through dead mirrors.
NITTER_HOSTS = ["https://xcancel.com"]

TIMEOUT = 8
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
    return " ".join(html.unescape(text).split()).strip()


def _extract_fx_media(status):
    media = status.get("media") or {}
    candidates = []
    if isinstance(media, dict):
        for key in ("all", "photos", "videos", "mosaic"):
            value = media.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        if media.get("url"):
            candidates.append(media)
    elif isinstance(media, list):
        candidates = media

    out = []
    seen = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("thumbnail_url")
        if not url or url in seen:
            continue
        seen.add(url)
        kind = str(item.get("type") or "photo").lower()
        if kind in ("video", "gif", "animated_gif"):
            kind = "video"
            variants = item.get("variants") or []
            mp4 = []
            for variant in variants if isinstance(variants, list) else []:
                if not isinstance(variant, dict):
                    continue
                vurl = variant.get("url")
                ctype = str(variant.get("content_type") or variant.get("contentType") or "")
                if vurl and ("mp4" in ctype.lower() or vurl.split("?")[0].lower().endswith(".mp4")):
                    mp4.append((int(variant.get("bitrate") or 0), vurl))
            if mp4:
                mp4.sort(reverse=True)
                url = mp4[0][1]
        else:
            kind = "photo"
        out.append({"type": kind, "url": str(url)})
    return out[:10]


def _fx_status_to_tweet(status):
    tid = str(status.get("id") or "").strip()
    if not tid.isdigit():
        return None
    author_obj = status.get("author") or {}
    username = str(author_obj.get("screen_name") or author_obj.get("username") or "unknown").lstrip("@")
    text = str(status.get("text") or "").strip() or "(post without text)"
    url = str(status.get("url") or f"https://x.com/{username}/status/{tid}")
    match = re.search(r"/([^/?#]+)/status/(\d+)", url)
    if match:
        username = match.group(1)
    return {
        "id": tid,
        "author": f"@{username}",
        "text": text,
        "url": f"https://x.com/{username}/status/{tid}",
        "created": snowflake_datetime(tid),
        "media": _extract_fx_media(status),
    }


def _fetch_fx(path, params, label, limit):
    response = requests.get(
        FX_BASE + path,
        params=params,
        timeout=FX_TIMEOUT,
        headers={"User-Agent": "PAO-Watcher/2.0", "Accept": "application/json"},
    )
    if response.status_code not in (200, 404):
        raise RuntimeError(f"FxTwitter {label} HTTP {response.status_code}")
    data = response.json()
    results = data.get("results") or []
    tweets = []
    seen = set()
    for status in results:
        if not isinstance(status, dict) or status.get("type") == "tombstone":
            continue
        tweet = _fx_status_to_tweet(status)
        if not tweet or tweet["id"] in seen:
            continue
        seen.add(tweet["id"])
        tweets.append(tweet)
        if len(tweets) >= limit:
            break
    tweets.sort(key=lambda item: int(item["id"]), reverse=True)
    if not tweets:
        raise RuntimeError(f"FxTwitter {label} returned 0 posts")
    print(
        f"X FX {label}: {len(tweets)} posts; latest={tweets[0]['created'].isoformat()}",
        flush=True,
    )
    return tweets


def _fetch_fx_user(username, limit=40):
    username = str(username).strip().lstrip("@")
    return _fetch_fx(
        f"/profile/{quote(username, safe='')}/statuses",
        {"count": min(max(int(limit), 1), 100)},
        f"user @{username}",
        limit,
    )


def _fetch_fx_keyword(query, limit=40):
    return _fetch_fx(
        "/search",
        {"q": str(query), "feed": "latest", "count": min(max(int(limit), 1), 100)},
        f"search {query!r}",
        limit,
    )


def _item_text(item):
    title = item.findtext("title") or ""
    description = item.findtext("description") or ""
    return _strip_html(title) or _strip_html(description) or "(post without text)"


def _status_match(value):
    value = str(value or "")
    return re.search(
        r"https?://[^/]+/([^/?#]+)/status/(\d+)", value, flags=re.I
    )


def _parse_rss(content, limit=100):
    if isinstance(content, bytes):
        content = content.lstrip(b"\xef\xbb\xbf \t\r\n")
    else:
        content = str(content or "").lstrip("\ufeff \t\r\n")
    root = ET.fromstring(content)
    found = {}
    for item in root.findall(".//item"):
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        match = _status_match(link) or _status_match(ET.tostring(item, encoding="unicode"))
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
        "User-Agent": "PAO-Watcher-X-RSS-Fallback/1.4",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    for host in hosts:
        try:
            response = requests.get(host.rstrip("/") + path, timeout=TIMEOUT, headers=headers)
            if response.status_code != 200:
                errors.append(f"{host} HTTP {response.status_code}")
                continue
            tweets = _parse_rss(response.content, limit=limit)
            if not tweets:
                errors.append(f"{host} empty feed")
                continue
            if max_age is not None and datetime.now(timezone.utc) - tweets[0]["created"] > max_age:
                errors.append(f"{host} stale feed latest={tweets[0]['created'].isoformat()}")
                continue
            print(f"X RSS {label}: {len(tweets)} posts via {host}", flush=True)
            return tweets
        except Exception as exc:
            errors.append(f"{host} {type(exc).__name__}: {exc}")
    raise RuntimeError(f"X RSS feed failed for {label}: " + "; ".join(errors))


def _fetch_rss_user(username, limit=40):
    safe = quote(str(username).strip().lstrip("@"), safe="")
    try:
        return _fetch_path(
            f"/twitter/user/{safe}/exclude_rts_replies", USER_HOSTS,
            f"RSSHub user @{safe}", limit, USER_MAX_AGE,
        )
    except Exception as rss_error:
        print(f"RSSHub user @{safe} failed: {rss_error}; trying Nitter", flush=True)
        return _fetch_path(f"/{safe}/rss", NITTER_HOSTS, f"Nitter user @{safe}", limit, USER_MAX_AGE)


def _fetch_rss_keyword(query, limit=40):
    safe = quote(str(query).strip(), safe="")
    try:
        return _fetch_path(
            f"/twitter/keyword/{safe}", KEYWORD_HOSTS,
            f"RSSHub keyword {query!r}", limit,
        )
    except Exception as rss_error:
        print(f"RSSHub keyword {query!r} failed: {rss_error}; trying Nitter", flush=True)
        return _fetch_path(
            f"/search/rss?f=tweets&q={safe}", NITTER_HOSTS,
            f"Nitter keyword {query!r}", limit,
        )


def fetch_user(username, limit=40):
    try:
        return _fetch_fx_user(username, limit)
    except Exception as fx_error:
        print(f"FxTwitter user @{str(username).lstrip('@')} failed: {fx_error}; trying RSS", flush=True)
        return _fetch_rss_user(username, limit)


def fetch_keyword(query, limit=40):
    try:
        return _fetch_fx_keyword(query, limit)
    except Exception as fx_error:
        print(f"FxTwitter search {query!r} failed: {fx_error}; trying RSS", flush=True)
        return _fetch_rss_keyword(query, limit)


def fetch_many_keywords(queries, limit=100):
    unique = []
    for query in queries:
        query = str(query or "").strip()
        if query and query not in unique:
            unique.append(query)
    found = {}
    errors = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(unique)))) as pool:
        futures = {pool.submit(fetch_keyword, q, min(limit, 40)): q for q in unique}
        for future in as_completed(futures):
            query = futures[future]
            try:
                for tweet in future.result():
                    found[tweet["id"]] = tweet
            except Exception as exc:
                errors.append(f"{query!r}: {exc}")
    tweets = sorted(found.values(), key=lambda item: int(item["id"]), reverse=True)[:limit]
    if not tweets:
        raise RuntimeError("X feed returned 0 posts: " + "; ".join(errors[-4:]))
    return tweets


def fetch_general(limit=100):
    return fetch_keyword(GENERAL_QUERY, limit=limit)
