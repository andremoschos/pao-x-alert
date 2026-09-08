import asyncio
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright

AUTH = os.environ.get("X_AUTH_TOKEN", "")
CT0 = os.environ.get("X_CT0", "")

# Public web client bearer token used by X's web client / open-source readers.
PUBLIC_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

GQL_FEATURES = {
    "articles_preview_enabled": False,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": False,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_profile_redirect_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_awards_web_tipping_enabled": False,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "tweet_with_visibility_results_prefer_gql_media_interstitial_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": False,
    "responsive_web_grok_share_attachment_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_image_annotation_enabled": False,
    "responsive_web_grok_analysis_button_from_backend": False,
    "responsive_web_jetfuel_frame": False,
    "rweb_video_screen_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
}

SEARCH_OPS = [
    ("fxembed_current", "GcXk9vN_d1jUfHNqLacXQA"),
    ("twscrape_current", "hyPfJYJ_XAtDYoslQc-Rgg"),
]


def _collect_tweet_ids(obj):
    ids = set()

    def walk(value):
        if isinstance(value, dict):
            rest_id = str(value.get("rest_id") or "")
            legacy = value.get("legacy")
            if rest_id.isdigit() and isinstance(legacy, dict) and (
                "full_text" in legacy or "created_at" in legacy
            ):
                ids.add(rest_id)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)
    return sorted(ids, key=int, reverse=True)


def inspect_direct_search(host, op_label, op_id, query):
    variables = {
        "rawQuery": query,
        "count": 20,
        "product": "Latest",
        "querySource": "typed_query",
    }
    params = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(GQL_FEATURES, separators=(",", ":")),
        "fieldToggles": json.dumps({"withArticleRichContentState": False}, separators=(",", ":")),
    }
    if host == "api.x.com":
        base = f"https://api.x.com/graphql/{op_id}/SearchTimeline"
    else:
        base = f"https://x.com/i/api/graphql/{op_id}/SearchTimeline"
    url = base + "?" + urlencode(params)
    headers = {
        "Authorization": PUBLIC_BEARER,
        "Cookie": f"auth_token={AUTH}; ct0={CT0}",
        "x-csrf-token": CT0,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://x.com",
        "Referer": "https://x.com/search?q=Panathinaikos&src=typed_query&f=live",
    }
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            status = response.status
        data = json.loads(raw.decode("utf-8", errors="replace"))
        ids = _collect_tweet_ids(data)
        errors = data.get("errors") if isinstance(data, dict) else None
        print(
            "X_DIRECT_SEARCH",
            {
                "host": host,
                "op": op_label,
                "query": query,
                "status": status,
                "tweet_count": len(ids),
                "latest_ids": ids[:3],
                "has_errors": bool(errors),
                "error_codes": [e.get("code") for e in errors[:5]] if isinstance(errors, list) else [],
            },
            flush=True,
        )
        return bool(ids)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        errors = data.get("errors") if isinstance(data, dict) else None
        print(
            "X_DIRECT_SEARCH",
            {
                "host": host,
                "op": op_label,
                "query": query,
                "status": exc.code,
                "tweet_count": 0,
                "has_errors": bool(errors),
                "error_codes": [e.get("code") for e in errors[:5]] if isinstance(errors, list) else [],
            },
            flush=True,
        )
        return False
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(
            "X_DIRECT_SEARCH",
            {
                "host": host,
                "op": op_label,
                "query": query,
                "error": f"{type(exc).__name__}: {exc}",
            },
            flush=True,
        )
        return False


async def inspect_page(page, label, url):
    api_errors = []

    def on_response(response):
        if "/i/api/" in response.url and response.status >= 400:
            clean = response.url.split("?", 1)[0]
            api_errors.append((response.status, clean[-180:]))

    page.on("response", on_response)
    response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(8000)

    article_count = await page.locator("article").count()
    status_link_count = await page.locator('a[href*="/status/"]').count()
    home_link_count = await page.locator('a[href="/home"]').count()
    login_link_count = await page.locator('a[href="/login"]').count()
    primary_count = await page.locator('[data-testid="primaryColumn"]').count()
    body = (await page.locator("body").inner_text(timeout=5000)).lower()

    markers = {
        "panathinaikos": "panathinaikos" in body or "παναθηνα" in body,
        "log_in": "log in" in body or "sign in" in body,
        "something_wrong": "something went wrong" in body,
        "rate_limit": "rate limit" in body or "too many requests" in body,
        "try_again": "try again" in body,
    }

    print(
        "X_DIAG",
        label,
        {
            "http_status": response.status if response else None,
            "final_url": page.url.split("?", 1)[0],
            "title": await page.title(),
            "articles": article_count,
            "status_links": status_link_count,
            "home_link": home_link_count,
            "login_link": login_link_count,
            "primary_column": primary_count,
            "body_len": len(body),
            "markers": markers,
            "api_errors": api_errors[:12],
        },
        flush=True,
    )


async def main():
    if not AUTH or not CT0:
        raise RuntimeError("X_AUTH_TOKEN or X_CT0 is missing")

    direct_ok = False
    for query in ("Panathinaikos", "#Panathinaikos"):
        for host in ("api.x.com", "x.com"):
            for op_label, op_id in SEARCH_OPS:
                if inspect_direct_search(host, op_label, op_id, query):
                    direct_ok = True

    print("X_DIRECT_SEARCH_SUMMARY", {"any_working_route": direct_ok}, flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            viewport={"width": 1400, "height": 1100},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
            ),
        )
        await context.add_cookies(
            [
                {
                    "name": "auth_token",
                    "value": AUTH,
                    "domain": ".x.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                },
                {
                    "name": "ct0",
                    "value": CT0,
                    "domain": ".x.com",
                    "path": "/",
                    "secure": True,
                },
            ]
        )

        cookie_names = sorted(
            {cookie.get("name") for cookie in await context.cookies("https://x.com")}
        )
        print(
            "X_DIAG cookies_loaded=",
            [name for name in cookie_names if name in {"auth_token", "ct0"}],
            flush=True,
        )

        page = await context.new_page()
        await inspect_page(page, "home", "https://x.com/home")
        await inspect_page(
            page,
            "search_live",
            f"https://x.com/search?q={quote('Panathinaikos')}&src=typed_query&f=live",
        )
        await inspect_page(page, "paofc_profile", "https://x.com/paofc_")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
