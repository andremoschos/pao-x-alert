import os
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from twscrape import API, gather

STATE = Path("panathinaikos_seen.json")
ENGINE = "twscrape_panathinaikos_only_v1"
MAX_SEEN = 2000
QUERY = "panathinaikos"

AUTH = os.environ["X_AUTH_TOKEN"]
CT0 = os.environ["X_CT0"]
TOPIC = os.environ["NTFY_PANATHINAIKOS_TOPIC"]


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return {"engine": data.get("engine"),"initialized": bool(data.get("initialized", False)),"ids": [str(x) for x in data.get("ids", [])]}
    except Exception:
        return {"engine": None, "initialized": False, "ids": []}


def save_state(ids):
    ordered = sorted({str(x) for x in ids}, key=int)[-MAX_SEEN:]
    STATE.write_text(json.dumps({"engine": ENGINE,"initialized": True,"ids": ordered}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def snowflake_datetime(tweet_id: int) -> datetime:
    ms = (int(tweet_id) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def notify(tweet):
    url = f"https://ntfy.sh/{TOPIC}"
    headers = {"Title": "X PANATHINAIKOS", "Priority": "high", "Tags": "mag", "Click": tweet["url"]}
    body = f'{tweet["author"]}\n{tweet["text"]}\n{tweet["url"]}'
    r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()


async def fetch_latest():
    db = "/tmp/twscrape_panathinaikos_accounts.db"
    Path(db).unlink(missing_ok=True)
    api = API(db, raise_when_no_account=True, wait_timeout=30, wait_interval=1)
    cookie_header = f"auth_token={AUTH}; ct0={CT0}"
    await api.pool.add_account_cookies("newspao_panathinaikos", cookie_header)
    results = await gather(api.search(QUERY, limit=100))
    tweets, seen_now = [], set()
    for t in results:
        tid = str(t.id)
        if tid in seen_now:
            continue
        seen_now.add(tid)
        username = getattr(getattr(t, "user", None), "username", "") or "unknown"
        text = (getattr(t, "rawContent", "") or "").strip() or "(post without text)"
        tweets.append({"id": tid,"author": f"@{username}","text": text,"url": f"https://x.com/{username}/status/{tid}","created": snowflake_datetime(int(tid))})
    return tweets


async def main():
    state = load_state()
    previous = set(state["ids"])
    tweets = await fetch_latest()
    print(f"Panathinaikos search results: {len(tweets)}")
    if not tweets:
        raise RuntimeError("X Panathinaikos search returned 0 posts")
    if state["engine"] != ENGINE:
        previous.update(t["id"] for t in tweets)
        save_state(previous)
        print(f"Panathinaikos baseline saved: {len(tweets)} posts")
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
    print(f"Fresh Panathinaikos posts: {len(fresh)}")


if __name__ == "__main__":
    asyncio.run(main())
