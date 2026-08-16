import os, json, re, time
from datetime import datetime, timezone
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright

SEARCH_URL = "https://x.com/search?q=panathinaikos&src=typeahead_click&f=live"
STATE = Path("seen.json")
MAX_SEEN = 1500

AUTH = os.environ["X_AUTH_TOKEN"]
CT0 = os.environ["X_CT0"]
TOPIC = os.environ["NTFY_TOPIC"]

def load_seen():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return set(data.get("ids", [])), bool(data.get("initialized", False))
    except Exception:
        return set(), False

def save_seen(seen):
    STATE.write_text(json.dumps(
        {"initialized": True, "ids": list(seen)[-MAX_SEEN:]},
        ensure_ascii=False, indent=2
    ), encoding="utf-8")

def notify(tweet):
    url = f"https://ntfy.sh/{TOPIC}"
    headers = {
        "Title": "NEO PANATHINAIKOS POST",
        "Priority": "high",
        "Tags": "rotating_light",
        "Click": tweet["url"],
    }
    body = f'{tweet["author"]}\n{tweet["text"]}\n{tweet["url"]}'
    r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()

def main():
    seen, initialized = load_seen()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-US",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/151.0.0.0 Safari/537.36")
        )
        context.add_cookies([
            {"name": "auth_token", "value": AUTH, "domain": ".x.com", "path": "/", "secure": True, "httpOnly": True},
            {"name": "ct0", "value": CT0, "domain": ".x.com", "path": "/", "secure": True},
        ])

        page = context.new_page()
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)

        # If X redirects to login, the cookies need refreshing.
        if "/i/flow/login" in page.url or page.locator('input[autocomplete="username"]').count():
            raise RuntimeError("X_LOGIN_REQUIRED: refresh X_AUTH_TOKEN and X_CT0 GitHub secrets")

        articles = page.locator('article[data-testid="tweet"]')
        count = articles.count()
        tweets = []

        for i in range(min(count, 30)):
            article = articles.nth(i)
            href = None
            for j in range(article.locator('a[href*="/status/"]').count()):
                h = article.locator('a[href*="/status/"]').nth(j).get_attribute("href")
                if h and re.search(r"/status/\d+", h):
                    href = h
                    break
            if not href:
                continue

            m = re.match(r"^/([^/]+)/status/(\d+)", href)
            if not m:
                continue

            author, tid = "@" + m.group(1), m.group(2)
            text_node = article.locator('[data-testid="tweetText"]')
            text = text_node.first.inner_text().strip() if text_node.count() else "(post without text)"
            tweets.append({
                "id": tid,
                "author": author,
                "text": text,
                "url": "https://x.com" + href.split("?")[0],
            })

        browser.close()

    if not tweets:
        raise RuntimeError("No posts found. X may have changed the page or blocked the runner.")

    # First successful run sets baseline: no old-post spam.
    if not initialized:
        seen.update(t["id"] for t in tweets)
        save_seen(seen)
        print(f"Baseline saved: {len(tweets)} visible posts.")
        return

    fresh = [t for t in tweets if t["id"] not in seen]
    for t in reversed(fresh):
        notify(t)
        seen.add(t["id"])
        print("Sent:", t["url"])

    if fresh:
        save_seen(seen)
    print(f"Fresh posts: {len(fresh)}")

if __name__ == "__main__":
    main()
