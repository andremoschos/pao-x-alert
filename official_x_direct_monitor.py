import os
import re
import json
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import x_rsshub_fallback as rss_x
from twscrape import API, gather
from playwright.async_api import async_playwright

STATE = Path("official_seen.json")
FRESH_MINUTES = 120
FETCH_LIMIT = 40
MAX_SEEN = 10000

TOPIC = os.environ["NTFY_OFFICIAL_TOPIC"]
X_AUTH_TOKEN = os.environ["X_AUTH_TOKEN"]
X_CT0 = os.environ["X_CT0"]

X_SOURCES = [
    {"key": "x_pae", "org": "PAE", "username": "paofc_"},
    {"key": "x_kae", "org": "KAE", "username": "Paobcgr"},
    {"key": "x_ao", "org": "AO", "username": "acpanathinaikos"},
]


@dataclass
class SimpleUser:
    username: str


@dataclass
class SimpleTweet:
    id: int
    rawContent: str
    user: SimpleUser


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {
            "engine": "official_pao_13_sources_v1",
            "ids": [],
            "initialized_sources": [],
        }


def save_state(data):
    ids = sorted(set(str(x) for x in data.get("ids", [])))
    data["ids"] = ids[-MAX_SEEN:]
    initialized = set(str(x) for x in data.get("initialized_sources", []))
    initialized.update(source["key"] for source in X_SOURCES)
    data["initialized_sources"] = sorted(initialized)
    STATE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def snowflake_datetime(tweet_id):
    ms = (int(tweet_id) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def notify(source, tweet):
    username = getattr(getattr(tweet, "user", None), "username", "") or source["username"]
    text = (getattr(tweet, "rawContent", "") or "").strip()
    tweet_id = str(tweet.id)
    url = f"https://x.com/{username}/status/{tweet_id}"
    headers = {
        "Title": f"OFFICIAL PAO - {source['org']} - X",
        "Priority": "high",
        "Tags": "green_circle",
        "Click": url,
    }
    body = f"@{username}\n{text}\n{url}"
    r = requests.post(
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()


async def fetch_source_twscrape(api, source):
    user = await api.user_by_login(source["username"])
    if not user:
        raise RuntimeError(f"Could not resolve @{source['username']}")
    user_id = getattr(user, "id", None)
    if not user_id:
        raise RuntimeError(f"No user id returned for @{source['username']}")
    tweets = await gather(api.user_tweets_and_replies(user_id, limit=FETCH_LIMIT))
    out = []
    for tweet in tweets:
        username = getattr(getattr(tweet, "user", None), "username", "") or source["username"]
        if username.lower() == source["username"].lower():
            out.append(tweet)
    if not out:
        raise RuntimeError(f"twscrape returned 0 own posts for @{source['username']}")
    return out


async def fetch_source_browser(source):
    username = source["username"]
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
        await context.add_cookies([
            {
                "name": "auth_token",
                "value": X_AUTH_TOKEN,
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            },
            {
                "name": "ct0",
                "value": X_CT0,
                "domain": ".x.com",
                "path": "/",
                "secure": True,
            },
        ])
        page = await context.new_page()
        try:
            await page.goto(
                f"https://x.com/{username}",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await page.wait_for_timeout(5500)
            articles = page.locator("article")
            count = min(await articles.count(), 25)
            found = {}
            for i in range(count):
                article = articles.nth(i)
                try:
                    text = " ".join((await article.inner_text(timeout=3000)).split()).strip()
                except Exception:
                    text = ""
                links = article.locator('a[href*="/status/"]')
                for j in range(min(await links.count(), 20)):
                    href = (await links.nth(j).get_attribute("href") or "").strip()
                    m = re.search(rf"/{re.escape(username)}/status/(\d+)", href, flags=re.I)
                    if not m:
                        continue
                    tweet_id = m.group(1)
                    found[tweet_id] = SimpleTweet(
                        id=int(tweet_id),
                        rawContent=text,
                        user=SimpleUser(username=username),
                    )
                    break
            out = sorted(found.values(), key=lambda tweet: int(tweet.id), reverse=True)
            if not out:
                raise RuntimeError(f"browser returned 0 own status posts for @{username}")
            print(f"{source['key']} X browser fallback: {len(out)} posts", flush=True)
            return out
        finally:
            await browser.close()


async def fetch_source_rsshub(source):
    items = await asyncio.to_thread(rss_x.fetch_user, source["username"], FETCH_LIMIT)
    out = []
    for item in items:
        username = str(item.get("author") or "").strip().lstrip("@") or source["username"]
        out.append(
            SimpleTweet(
                id=int(item["id"]),
                rawContent=str(item.get("text") or "").strip(),
                user=SimpleUser(username=username),
            )
        )
    if not out:
        raise RuntimeError(f"RSSHub returned 0 own posts for @{source['username']}")
    print(f"{source['key']} X RSSHub fallback: {len(out)} posts", flush=True)
    return out


async def fetch_source(api, source):
    if api is not None:
        try:
            return await fetch_source_twscrape(api, source)
        except Exception as exc:
            print(
                f"{source['key']} twscrape failed: {exc}; trying Chromium profile fallback",
                flush=True,
            )
    else:
        print(
            f"{source['key']} twscrape unavailable; using Chromium profile fallback",
            flush=True,
        )

    try:
        return await fetch_source_browser(source)
    except Exception as exc:
        print(
            f"{source['key']} Chromium failed: {exc}; trying verified RSSHub user feed",
            flush=True,
        )
    return await fetch_source_rsshub(source)


async def make_api_or_none():
    db = "/tmp/twscrape_official_x_direct.db"
    Path(db).unlink(missing_ok=True)
    try:
        api = API(db, raise_when_no_account=True, wait_timeout=12, wait_interval=1)
        cookie_header = f"auth_token={X_AUTH_TOKEN}; ct0={X_CT0}"
        await api.pool.add_account_cookies("official-x-direct", cookie_header)
        return api
    except Exception as exc:
        print(
            f"Official X twscrape setup failed: {type(exc).__name__}: {exc}; "
            "using Chromium/RSSHub recovery",
            flush=True,
        )
        return None


async def main():
    state = load_state()
    seen = set(str(x) for x in state.get("ids", []))
    api = await make_api_or_none()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=FRESH_MINUTES)
    total_sent = 0
    successful_sources = 0

    for source in X_SOURCES:
        try:
            tweets = await fetch_source(api, source)
            successful_sources += 1
            fresh_recent = []
            for tweet in tweets:
                tweet_id = str(tweet.id)
                item_id = f"{source['key']}:{tweet_id}"
                if item_id in seen:
                    continue
                if snowflake_datetime(tweet_id) >= cutoff:
                    fresh_recent.append(tweet)
                else:
                    seen.add(item_id)

            for tweet in sorted(fresh_recent, key=lambda t: int(t.id)):
                item_id = f"{source['key']}:{tweet.id}"
                notify(source, tweet)
                seen.add(item_id)
                total_sent += 1
                print(
                    "OFFICIAL X DIRECT SENT:",
                    source["org"],
                    f"https://x.com/{source['username']}/status/{tweet.id}",
                    flush=True,
                )

            print(
                f"{source['key']} direct: {len(tweets)} fetched, "
                f"{len(fresh_recent)} recent unseen sent",
                flush=True,
            )
        except Exception as exc:
            print(f"{source['key']} direct error: {exc}", flush=True)

    state["ids"] = sorted(seen)
    save_state(state)
    print(
        f"Official X direct complete: {total_sent} notifications sent; "
        f"sources_ok={successful_sources}/{len(X_SOURCES)}",
        flush=True,
    )
    if successful_sources == 0:
        raise RuntimeError("Official X recovery failed for all sources")


if __name__ == "__main__":
    asyncio.run(main())
