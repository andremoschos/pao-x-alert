import html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests

FX_BASE = "https://api.fxtwitter.com/2"
FX_SEARCH_PROXY = "https://twitter.2-38.com/api/fx/2"
FX_TIMEOUT = 15

USER_HOSTS = ["https://rss.xxu.do", "https://rsshub.stsecurity.moe"]
KEYWORD_HOSTS = ["https://rsshub.stsecurity.moe", "https://rss.xxu.do"]
NITTER_HOSTS = ["https://xcancel.com"]

TIMEOUT = 8
USER_MAX_AGE = timedelta(days=14)

# X's authenticated web SearchTimeline is the PRIMARY path for keyword/Latest
# searches. GitHub-hosted Chromium is challenged by X, but the same session can
# read the GraphQL SearchTimeline endpoint directly. The query ID rotates, so a
# recent ID is tried first and an older known-good ID remains as a safe backup.
X_WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
X_SEARCH_QUERY_IDS = [
    "hyPfJYJ_XAtDYoslQc-Rgg",  # captured from late-Aug 2026 clients
    "GcXk9vN_d1jUfHNqLacXQA",  # older recovery ID
]
X_SEARCH_TIMEOUT = 20
X_SEARCH_FEATURES = {
    "articles_preview_enabled": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "post_ctas_fetch_enabled": True,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": True,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_profile_redirect_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "rweb_video_screen_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
}

# Emergency recovery only. This is NOT a substitute for global Latest search.
PAO_TIMELINE_ACCOUNTS = [
    "paofc_",
    "Paobcgr",
    "acpanathinaikos",
    "paobc",
    "fmeetsdata",
    "Onlypao_gr",
    "papanikolaouchs",
]

# Search the two user-visible X "Latest" queries independently. A single large
# OR query can crowd one variant out of the first page and silently miss posts.
GENERAL_LATEST_QUERIES = [
    "Panathinaikos",
    "#Panathinaikos",
    '"παναθηναϊκός" OR "παναθηναϊκού" OR "παναθηναϊκό" OR '
    '"παναθηναικος" OR "παναθηναικου" OR "παναθηναικο"',
]

# General and Only-PAO run in the same fast process. Reuse identical Latest
# results briefly so both lanes get full coverage without doubling X requests.
_KEYWORD_CACHE_TTL_SECONDS = 90
_KEYWORD_CACHE = {}
_KEYWORD_CACHE_LOCK = threading.Lock()


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

    out, seen = [], set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("thumbnail_url")
        if not url:
            continue
        kind = str(item.get("type") or "photo").lower()
        if kind in ("video", "gif", "animated_gif"):
            kind = "video"
            mp4 = []
            for variant in item.get("variants") or []:
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
        if url in seen:
            continue
        seen.add(url)
        out.append({"type": kind, "url": str(url)})
    return out[:10]


def _fx_status_to_tweet(status):
    tid = str(status.get("id") or "").strip()
    if not tid.isdigit():
        return None
    author_obj = status.get("author") or {}
    username = str(author_obj.get("screen_name") or author_obj.get("username") or "unknown").lstrip("@")
    text = str(status.get("text") or "").strip() or "(post without text)"
    source_url = str(status.get("url") or "")
    match = re.search(r"/([^/?#]+)/status/(\d+)", source_url)
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


def _fetch_fx_from(base, path, params, label, limit):
    response = requests.get(
        base.rstrip("/") + path,
        params=params,
        timeout=FX_TIMEOUT,
        headers={"User-Agent": "PAO-Watcher/2.4", "Accept": "application/json"},
    )
    if response.status_code not in (200, 404):
        raise RuntimeError(f"{label} HTTP {response.status_code}")
    data = response.json()
    results = (data.get("results") or []) if isinstance(data, dict) else []
    tweets, seen = [], set()
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
        message = str(data.get("message") or "")[:120] if isinstance(data, dict) else ""
        raise RuntimeError(f"{label} returned 0 posts {message}".strip())
    print(f"X API {label}: {len(tweets)} posts; latest={tweets[0]['created'].isoformat()}", flush=True)
    return tweets


def _fetch_fx_user(username, limit=40):
    username = str(username).strip().lstrip("@")
    return _fetch_fx_from(
        FX_BASE,
        f"/profile/{quote(username, safe='')}/statuses",
        {"count": min(max(int(limit), 1), 100)},
        f"FxTwitter user @{username}",
        limit,
    )


def fetch_users(usernames=None, limit=100):
    usernames = list(usernames or PAO_TIMELINE_ACCOUNTS)
    found, errors = {}, []
    with ThreadPoolExecutor(max_workers=min(7, max(1, len(usernames)))) as pool:
        futures = {pool.submit(fetch_user, username, min(30, limit)): username for username in usernames}
        for future in as_completed(futures):
            username = futures[future]
            try:
                for tweet in future.result():
                    found[tweet["id"]] = tweet
            except Exception as exc:
                errors.append(f"@{username}: {exc}")
    tweets = sorted(found.values(), key=lambda item: int(item["id"]), reverse=True)[:limit]
    if not tweets:
        raise RuntimeError("PAO timeline aggregation returned 0 posts: " + "; ".join(errors[-7:]))
    print(f"X PAO timeline aggregation RECOVERY: {len(tweets)} posts from {len(usernames)} accounts", flush=True)
    return tweets


def _extract_graphql_media(legacy):
    entities = (legacy or {}).get("extended_entities") or (legacy or {}).get("entities") or {}
    out, seen = [], set()
    for item in entities.get("media") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "photo").lower()
        if kind == "photo":
            url = item.get("media_url_https") or item.get("media_url")
            if url and url not in seen:
                seen.add(url)
                out.append({"type": "photo", "url": str(url)})
            continue

        variants = ((item.get("video_info") or {}).get("variants") or [])
        mp4 = []
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            url = variant.get("url")
            ctype = str(variant.get("content_type") or "")
            if url and "mp4" in ctype.lower():
                mp4.append((int(variant.get("bitrate") or 0), str(url)))
        if mp4:
            mp4.sort(reverse=True)
            url = mp4[0][1]
            if url not in seen:
                seen.add(url)
                out.append({"type": "video", "url": url})
    return out[:10]


def _screen_name_from_user(user_result):
    if not isinstance(user_result, dict):
        return ""
    legacy = user_result.get("legacy") or {}
    core = user_result.get("core") or {}
    for value in (
        legacy.get("screen_name"),
        core.get("screen_name"),
        user_result.get("screen_name"),
        user_result.get("username"),
    ):
        if value:
            return str(value).lstrip("@")
    return ""


def _graphql_result_to_tweet(result):
    if not isinstance(result, dict):
        return None
    # Some wrappers put the actual tweet one level down.
    if isinstance(result.get("tweet"), dict):
        result = result["tweet"]

    tid = str(result.get("rest_id") or "").strip()
    legacy = result.get("legacy") or {}
    if not tid.isdigit() or not isinstance(legacy, dict):
        return None

    text = str(legacy.get("full_text") or legacy.get("text") or "").strip()
    note = (((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {})
    if isinstance(note, dict) and note.get("text"):
        text = str(note.get("text")).strip()
    if not text:
        return None

    user_result = ((((result.get("core") or {}).get("user_results") or {}).get("result")) or {})
    username = _screen_name_from_user(user_result) or "unknown"
    return {
        "id": tid,
        "author": f"@{username}",
        "text": text,
        "url": f"https://x.com/{username}/status/{tid}",
        "created": snowflake_datetime(tid),
        "media": _extract_graphql_media(legacy),
    }


def _parse_search_timeline(data, limit=100):
    found = {}

    def walk(value):
        if isinstance(value, dict):
            tweet_results = value.get("tweet_results")
            if isinstance(tweet_results, dict):
                tweet = _graphql_result_to_tweet(tweet_results.get("result"))
                if tweet:
                    found[tweet["id"]] = tweet
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return sorted(found.values(), key=lambda item: int(item["id"]), reverse=True)[:limit]


def _fetch_x_authenticated_keyword(query, limit=40):
    auth = os.environ.get("X_AUTH_TOKEN", "").strip()
    ct0 = os.environ.get("X_CT0", "").strip()
    if not auth or not ct0:
        raise RuntimeError("X authenticated search unavailable: X_AUTH_TOKEN/X_CT0 missing")

    variables = {
        "rawQuery": str(query),
        "count": min(max(int(limit), 1), 40),
        "querySource": "typed_query",
        "product": "Latest",
    }
    payload = {
        "variables": variables,
        "features": X_SEARCH_FEATURES,
        "fieldToggles": {"withArticleRichContentState": False},
    }
    headers = {
        "Authorization": "Bearer " + X_WEB_BEARER,
        "Cookie": f"auth_token={auth}; ct0={ct0}",
        "x-csrf-token": ct0,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://x.com",
        "Referer": "https://x.com/search",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    errors = []
    for query_id in X_SEARCH_QUERY_IDS:
        url = f"https://x.com/i/api/graphql/{query_id}/SearchTimeline"
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=X_SEARCH_TIMEOUT)
            if response.status_code != 200:
                errors.append(f"{query_id[:8]} HTTP {response.status_code}")
                continue
            data = response.json()
            tweets = _parse_search_timeline(data, limit=limit)
            if not tweets:
                errors.append(f"{query_id[:8]} HTTP 200 but 0 parsed posts")
                continue
            print(
                f"X AUTHENTICATED LATEST search {query!r}: {len(tweets)} posts; "
                f"latest={tweets[0]['created'].isoformat()} qid={query_id[:8]}",
                flush=True,
            )
            return tweets
        except Exception as exc:
            errors.append(f"{query_id[:8]} {type(exc).__name__}: {exc}")

    raise RuntimeError("X authenticated Latest search failed: " + "; ".join(errors[-4:]))


def _fetch_fx_keyword(query, limit=40):
    params = {"q": str(query), "feed": "latest", "count": min(max(int(limit), 1), 100)}
    errors = []
    for base, label in ((FX_BASE, "FxTwitter search"), (FX_SEARCH_PROXY, "X search proxy")):
        try:
            return _fetch_fx_from(base, "/search", params, f"{label} {query!r}", limit)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    raise RuntimeError("; ".join(errors))


def _item_text(item):
    title = item.findtext("title") or ""
    description = item.findtext("description") or ""
    return _strip_html(title) or _strip_html(description) or "(post without text)"


def _status_match(value):
    return re.search(r"https?://[^/]+/([^/?#]+)/status/(\d+)", str(value or ""), flags=re.I)


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
        "User-Agent": "PAO-Watcher-X-RSS-Fallback/1.7",
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
            return tweets
        except Exception as exc:
            errors.append(f"{host} {type(exc).__name__}: {exc}")
    raise RuntimeError(f"X RSS feed failed for {label}: " + "; ".join(errors))


def _fetch_rss_user(username, limit=40):
    safe = quote(str(username).strip().lstrip("@"), safe="")
    try:
        return _fetch_path(
            f"/twitter/user/{safe}/exclude_rts_replies",
            USER_HOSTS,
            f"RSSHub user @{safe}",
            limit,
            USER_MAX_AGE,
        )
    except Exception as rss_error:
        print(f"RSSHub user @{safe} failed: {rss_error}; trying Nitter", flush=True)
        return _fetch_path(f"/{safe}/rss", NITTER_HOSTS, f"Nitter user @{safe}", limit, USER_MAX_AGE)


def _fetch_rss_keyword(query, limit=40):
    safe = quote(str(query).strip(), safe="")
    try:
        return _fetch_path(f"/twitter/keyword/{safe}", KEYWORD_HOSTS, f"RSSHub keyword {query!r}", limit)
    except Exception as rss_error:
        print(f"RSSHub keyword {query!r} failed: {rss_error}; trying Nitter", flush=True)
        return _fetch_path(f"/search/rss?f=tweets&q={safe}", NITTER_HOSTS, f"Nitter keyword {query!r}", limit)


def fetch_user(username, limit=40):
    try:
        return _fetch_fx_user(username, limit)
    except Exception as fx_error:
        print(f"FxTwitter user @{str(username).lstrip('@')} failed: {fx_error}; trying RSS", flush=True)
        return _fetch_rss_user(username, limit)


def _keyword_cache_key(query, limit):
    return (str(query or "").strip(), min(max(int(limit), 1), 40))


def _keyword_cache_get(query, limit):
    key = _keyword_cache_key(query, limit)
    now = time.monotonic()
    with _KEYWORD_CACHE_LOCK:
        entry = _KEYWORD_CACHE.get(key)
        if not entry:
            return None
        stored_at, tweets = entry
        if now - stored_at > _KEYWORD_CACHE_TTL_SECONDS:
            _KEYWORD_CACHE.pop(key, None)
            return None
        return [dict(item) for item in tweets]


def _keyword_cache_put(query, limit, tweets):
    key = _keyword_cache_key(query, limit)
    with _KEYWORD_CACHE_LOCK:
        _KEYWORD_CACHE[key] = (time.monotonic(), [dict(item) for item in tweets])


def fetch_keyword(query, limit=40):
    # PRIMARY: true authenticated X Latest search across ALL accounts.
    cached = _keyword_cache_get(query, limit)
    if cached:
        print(f"X Latest cache hit {query!r}: {len(cached)} posts", flush=True)
        return cached

    try:
        tweets = _fetch_x_authenticated_keyword(query, limit)
        _keyword_cache_put(query, limit, tweets)
        return tweets
    except Exception as auth_error:
        print(f"X authenticated Latest search {query!r} failed: {auth_error}; trying public recovery", flush=True)

    try:
        tweets = _fetch_fx_keyword(query, limit)
        _keyword_cache_put(query, limit, tweets)
        return tweets
    except Exception as fx_error:
        print(f"X public search {query!r} failed: {fx_error}; trying RSS", flush=True)
        tweets = _fetch_rss_keyword(query, limit)
        _keyword_cache_put(query, limit, tweets)
        return tweets


def fetch_many_keywords(queries, limit=100):
    unique = []
    for query in queries:
        query = str(query or "").strip()
        if query and query not in unique:
            unique.append(query)

    found, errors = {}, []
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
    if tweets:
        return tweets

    # Emergency recovery only: keep known PAO timelines alive, but log clearly
    # that this is degraded and not equivalent to global Latest search.
    normalized = " ".join(str(q or "").lower() for q in unique)
    if "panathinaikos" in normalized or "παναθη" in normalized:
        print(
            "X GLOBAL LATEST DEGRADED: all keyword searches failed; using curated PAO timelines recovery",
            flush=True,
        )
        return fetch_users(PAO_TIMELINE_ACCOUNTS, limit)

    raise RuntimeError("X feed returned 0 posts: " + "; ".join(errors[-4:]))


def fetch_general(limit=100):
    # True X "Latest" semantics: query the visible Panathinaikos terms separately
    # and merge by tweet id. Curated accounts are only the final recovery path.
    tweets = fetch_many_keywords(GENERAL_LATEST_QUERIES, limit)
    authors = {item["author"].lstrip("@").lower() for item in tweets}
    print(
        f"X GLOBAL LATEST general aggregation: {len(tweets)} posts from {len(authors)} authors",
        flush=True,
    )
    return tweets
