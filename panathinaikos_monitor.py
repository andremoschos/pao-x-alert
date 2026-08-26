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

STATE = Path("panathinaikos_seen.json")
ENGINE = "twscrape_panathinaikos_only_v1"
MAX_SEEN = 5000

# X occasionally returns an empty SearchTimeline for the bare lowercase term
# even while the Latest tab visibly contains results. Try an explicit exact/
# hashtag query first, then two compact fallbacks before using Chromium.
SEARCH_QUERIES = [
    '"Panathinaikos" OR #Panathinaikos',
    'Panathinaikos',
    '#Panathinaikos',
]
FETCH_LIMIT = 40
FRESH_MINUTES = 120

AUTH = os.environ["X_AUTH_TOKEN"]
CT0 = os.environ["X_CT0"]
TOPIC = os.environ["NTFY_PANATHINAIKOS_TOPIC"]


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
        "Title": "X PANATHINAIKOS",
        "Priority": "high",
        "Tags": "mag",
        "Click": tweet["url"],
    }
    body = f'{tweet["author"]}\n{tweet["text"]}\n{tweet["url"]}'

    last_response = None
    for attempt in range(1, 5):
        r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=20)
        last_response = r
        if 200 <= r.status_code < 300:
            return
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After", "")
            try:
                delay = float(retry_after)
            except Exception:
                delay = 5.0 * attempt
            delay = max(3.0, min(delay, 20.0))
            print(f"ntfy 429 Only Panathinaikos X; retry {attempt}/4 in {delay:.1f}s", flush=True)
            time.sleep(delay)
            continue
        if 500 <= r.status_code < 600:
            delay = 3.0 * attempt
            print(f"ntfy {r.status_code} Only Panathinaikos X; retry {attempt}/4 in {delay:.1f}s", flush=True)
            time.sleep(delay)
            continue
        r.raise_for_status()

    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError("ntfy delivery failed after retries")


def _tweet_from_result(t):
    tid = str(t.id)
    username = getattr(getattr(t, "user", None), "username", "") or "unknown"
    text = (getattr(t, "rawContent", "") or "").strip() or "(post without text)"
    return {
        "id": tid,
        "author": f"@{username}",
        "text": text,
        "url": f"https://x.com/{username}/status/{tid}",
        "created": snowflake_datetime(int(tid)),
    }


async def fetch_latest_twscrape():
    db = "/tmp/twscrape_panathinaikos_accounts.db"
    Path(db).unlink(missing_ok=True)
    api = API(db, raise_when_no_account=True, wait_timeout=12, wait_interval=1)
    cookie_header = f"auth_token={AUTH}; ct0={CT0}"
    await api.pool.add_account_cookies("newspao_panathinaikos", cookie_header)

    found = {}
    errors = []

    for index, query in enumerate(SEARCH_QUERIES):
        try:
            results = await gather(api.search(query, limit=FETCH_LIMIT))
        except Exception as exc:
            errors.append(f"{query!r}: {type(exc).__name__}: {exc}")
            continue

        for t in results:
            tweet = _tweet_from_result(t)
            found[tweet["id"]] = tweet

        print(
            f"Panathinaikos twscrape query {query!r}: {len(results)} results",
            flush=True,
        )

        # The first explicit query is enough when healthy. Only spend extra X
        # requests when the prior attempt actually returned nothing.
        if found:
            break

        if index < len(SEARCH_QUERIES) - 1:
            await asyncio.sleep(1)

    tweets = sorted(found.values(), key=lambda item: int(item["id"]), reverse=True)
    if not tweets:
        detail = "; ".join(errors[-3:]) or "all queries returned 0 posts"
        raise RuntimeError(f"twscrape Panathinaikos search empty: {detail}")
    return tweets


async def _browser_query(page, query):
    url = f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"
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

    return sorted(found.values(), key=lambda item: int(item["id"]), reverse=True)


async def fetch_latest_browser():
    """Authenticated Chromium fallback for the live Panathinaikos X search."""
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
            for query in SEARCH_QUERIES:
                tweets = await _browser_query(page, query)
                print(
                    f"Panathinaikos Chromium query {query!r}: {len(tweets)} results",
                    flush=True,
                )
                if tweets:
                    return tweets
            raise RuntimeError("Chromium Panathinaikos search returned 0 posts for all recovery queries")
        finally:
            await browser.close()


async def fetch_latest():
    try:
        return await fetch_latest_twscrape()
    except Exception as exc:
        print(
            f"Panathinaikos twscrape failed: {exc}; trying Chromium search fallback",
            flush=True,
        )
        return await fetch_latest_browser()


async def main():
    state = load_state()
    previous = set(state["ids"])
    tweets = await fetch_latest()
    print(f"Panathinaikos search results: {len(tweets)}")

    if state["engine"] != ENGINE:
        previous.update(t["id"] for t in tweets)
        save_state(previous)
        print(f"Panathinaikos baseline saved: {len(tweets)} posts")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=FRESH_MINUTES)
    fresh = [t for t in tweets if t["id"] not in previous and t["created"] >= cutoff]
    fresh.sort(key=lambda x: x["created"])

    for t in fresh:
        notify(t)
        previous.add(t["id"])
        print("Sent:", t["url"])

    # Keep every result in state after this cycle. The 2-hour recovery window
    # protects delayed indexing, while persistent IDs prevent duplicates.
    previous.update(t["id"] for t in tweets)
    save_state(previous)
    print(f"Fresh Panathinaikos posts: {len(fresh)}")


if __name__ == "__main__":
    asyncio.run(main())
