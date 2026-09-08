import asyncio
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright

AUTH = os.environ.get("X_AUTH_TOKEN", "")
CT0 = os.environ.get("X_CT0", "")

PUBLIC_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Current X web feature baseline (safe read-only SearchTimeline request).
GQL_FEATURES = {
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

# Recent known IDs. X rotates these, so the diagnostic tries more than one.
SEARCH_OPS = [
    ("2026-08-twscrape", "hyPfJYJ_XAtDYoslQc-Rgg"),
    ("2026-06-web", "GcXk9vN_d1jUfHNqLacXQA"),
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


def inspect_direct_search(op_label, op_id, query):
    variables = {
        "rawQuery": query,
        "count": 20,
        "querySource": "typed_query",
        "product": "Latest",
    }
    payload = json.dumps(
        {
            "variables": variables,
            "features": GQL_FEATURES,
            "fieldToggles": {"withArticleRichContentState": False},
        },
        separators=(",", ":"),
    ).encode("utf-8")

    url = f"https://x.com/i/api/graphql/{op_id}/SearchTimeline"
    headers = {
        "Authorization": PUBLIC_BEARER,
        "Cookie": f"auth_token={AUTH}; ct0={CT0}",
        "x-csrf-token": CT0,
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
        "Referer": f"https://x.com/search?q={quote(query)}&src=typed_query&f=live",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    request = Request(url, data=payload, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            status = response.status
        data = json.loads(raw.decode("utf-8", errors="replace"))
        ids = _collect_tweet_ids(data)
        errors = data.get("errors") if isinstance(data, dict) else None
        print(
            "X_POST_SEARCH",
            {
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
            "X_POST_SEARCH",
            {
                "op": op_label,
                "query": query,
                "status": exc.code,
                "tweet_count": 0,
                "has_errors": bool(errors),
                "error_codes": [e.get("code") for e in errors[:5]] if isinstance(errors, list) else [],
                "body_prefix": " ".join(body[:160].split()),
            },
            flush=True,
        )
        return False
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(
            "X_POST_SEARCH",
            {"op": op_label, "query": query, "error": f"{type(exc).__name__}: {exc}"},
            flush=True,
        )
        return False


async def inspect_page(page, label, url):
    response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(5000)
    body = (await page.locator("body").inner_text(timeout=5000)).lower()
    print(
        "X_DIAG",
        label,
        {
            "http_status": response.status if response else None,
            "final_url": page.url.split("?", 1)[0],
            "title": await page.title(),
            "articles": await page.locator("article").count(),
            "status_links": await page.locator('a[href*="/status/"]').count(),
            "body_len": len(body),
        },
        flush=True,
    )


async def main():
    if not AUTH or not CT0:
        raise RuntimeError("X_AUTH_TOKEN or X_CT0 is missing")

    direct_results = {}
    for query in ("Panathinaikos", "#Panathinaikos"):
        direct_results[query] = False
        for op_label, op_id in SEARCH_OPS:
            if inspect_direct_search(op_label, op_id, query):
                direct_results[query] = True
                break

    print("X_POST_SEARCH_SUMMARY", direct_results, flush=True)

    # Browser probe remains only as a secondary diagnostic for the known 403 challenge.
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
                {"name": "auth_token", "value": AUTH, "domain": ".x.com", "path": "/", "secure": True, "httpOnly": True},
                {"name": "ct0", "value": CT0, "domain": ".x.com", "path": "/", "secure": True},
            ]
        )
        page = await context.new_page()
        await inspect_page(page, "search_live", f"https://x.com/search?q={quote('Panathinaikos')}&src=typed_query&f=live")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
