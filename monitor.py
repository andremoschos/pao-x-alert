import os
import re
import json
import asyncio
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
from twscrape import API, gather
from playwright.async_api import async_playwright

STATE = Path("seen.json")
ENGINE = "twscrape_v3_full_terms"
MAX_SEEN = 2000
QUERY = (
    '"παναθηναϊκός" OR "παναθηναϊκού" OR "παναθηναϊκό" OR '
    '"παναθηναικος" OR "παναθηναικου" OR "παναθηναικο" OR '
    'from:paobc OR from:fmeetsdata'
)

AUTH = os.environ["X_AUTH_TOKEN"]
CT0 = os.environ["X_CT0"]
TOPIC = os.environ["NTFY_TOPIC"]


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return {
            "engine": data.get("engine"),
            "initialized": bool(data.get("initialized", False)),
            "ids": [str(x) for x in data.get("ids", [])],
        }
    except Exception:
        return {"engine": None, "initialized": False, "ids": []}


def save_state(ids):
    ordered = sorted({str(x) for x in ids}, key=int)[-MAX_SEEN:]
    STATE.write_text(
        json.dumps(
            {"engine": ENGINE, "initialized": True, "ids": ordered},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def snowflake_datetime(tweet_id: int) -> datetime:
    ms = (int(tweet_id) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def notify(tweet):
    url = f"https://ntfy.sh/{TOPIC}"
    headers = {
        "Title": "NEO PANATHINAIKOS POST",
        "Priority": "high",
        "Tags": "rotating_light",
        "Click": tweet["url"],
    }
    body = f'{tweet["author"]}\n{tweet["text"]}\n{tweet["url"]}'

    # The Fast runner owns ntfy pacing/retries globally. Keep exactly one send
    # here so a 429 cannot be amplified by a second nested retry loop.
    r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()


async def fetch_latest_twscrape():
    db = "/tmp/twscrape_accounts.db"
    Path(db).unlink(missing_ok=True)
    api = API(db, raise_when_no_account=True, wait_timeout=12, wait_interval=1)
    cookie_header = f"auth_token={AUTH}; ct0={CT0}"
    await api.pool.add_account_cookies("newspao", cookie_header)
    results = await gather(api.search(QUERY, limit=100))

    tweets = []
    seen_now = set()
    for t in results:
        tid = str(t.id)
        if tid in seen_now:
            continue
        seen_now.add(tid)
        username = getattr(getattr(t, "user", None), "username", "") or "unknown"
        text = (getattr(t, "rawContent", "") or "").strip() or "(post without text)"
        tweets.append(
            {
                "id": tid,
                "author": f"@{username}",
                "text": text,
                "url": f"https://x.com/{username}/status/{tid}",
                "created": snowflake_datetime(int(tid)),
            }
        )
    if not tweets:
        raise RuntimeError("twscrape X search returned 0 posts")
    return tweets


async def fetch_latest_browser():
    """Authenticated Chromium fallback for X search when twscrape/GraphQL breaks."""
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
        try:
            url = f"https://x.com/search?q={quote(QUERY)}&src=typed_query&f=live"
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(6000)
            articles = page.locator("article")
            count = min(await articles.count(), 40)
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
                    match = re.match(r"^/([^/]+)/status/(\d+)", href)
                    if not match:
                        continue
                    username, tid = match.group(1), match.group(2)
                    found[tid] = {
                        "id": tid,
                        "author": f"@{username}",
                        "text": text or "(post without text)",
                        "url": f"https://x.com/{username}/status/{tid}",
                        "created": snowflake_datetime(int(tid)),
                    }
                    break

            tweets = sorted(found.values(), key=lambda item: int(item["id"]), reverse=True)
            if not tweets:
                raise RuntimeError("Chromium X search returned 0 posts")
            print(f"X browser fallback results: {len(tweets)}", flush=True)
            return tweets
        finally:
            await browser.close()


async def fetch_latest():
    try:
        return await fetch_latest_twscrape()
    except Exception as exc:
        print(f"X twscrape failed: {exc}; trying Chromium search fallback", flush=True)
        return await fetch_latest_browser()


async def main():
    state = load_state()
    previous = set(state["ids"])
    tweets = await fetch_latest()
    print(f"Search results: {len(tweets)}")

    if state["engine"] != ENGINE:
        previous.update(t["id"] for t in tweets)
        save_state(previous)
        print(f"Fixed scanner baseline saved: {len(tweets)} posts")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=45)
    fresh = [t for t in tweets if t["id"] not in previous and t["created"] >= cutoff]
    fresh.sort(key=lambda x: x["created"])

    for t in fresh:
        notify(t)
        previous.add(t["id"])
        print("Sent:", t["url"])

    previous.update(t["id"] for t in tweets)
    save_state(previous)
    print(f"Fresh posts: {len(fresh)}")


if __name__ == "__main__":
    asyncio.run(main())
