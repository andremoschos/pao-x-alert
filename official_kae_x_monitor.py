import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from twscrape import API, gather


STATE = Path("official_seen.json")
USERNAME = "Paobcgr"
SOURCE_KEY = "x_kae"
ORG = "KAE"
FRESH_MINUTES = 90
FETCH_LIMIT = 40

TOPIC = os.environ["NTFY_OFFICIAL_TOPIC"]
X_AUTH_TOKEN = os.environ["X_AUTH_TOKEN"]
X_CT0 = os.environ["X_CT0"]


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
    data["ids"] = ids[-10000:]
    initialized = set(str(x) for x in data.get("initialized_sources", []))
    initialized.add(SOURCE_KEY)
    data["initialized_sources"] = sorted(initialized)

    STATE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def snowflake_datetime(tweet_id):
    ms = (int(tweet_id) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def notify(tweet):
    username = (
        getattr(getattr(tweet, "user", None), "username", "")
        or USERNAME
    )
    text = (getattr(tweet, "rawContent", "") or "").strip()
    tweet_id = str(tweet.id)
    url = f"https://x.com/{username}/status/{tweet_id}"

    headers = {
        "Title": "OFFICIAL PAO - KAE - X",
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


async def main():
    state = load_state()
    seen = set(str(x) for x in state.get("ids", []))

    db = "/tmp/twscrape_official_kae_direct.db"
    Path(db).unlink(missing_ok=True)

    api = API(
        db,
        raise_when_no_account=True,
        wait_timeout=30,
        wait_interval=1,
    )

    cookie_header = f"auth_token={X_AUTH_TOKEN}; ct0={X_CT0}"
    await api.pool.add_account_cookies(
        "official-kae-direct",
        cookie_header,
    )

    user = await api.user_by_login(USERNAME)
    if not user:
        raise RuntimeError(f"Could not resolve @{USERNAME}")

    user_id = getattr(user, "id", None)
    if not user_id:
        raise RuntimeError(f"No user id returned for @{USERNAME}")

    tweets = await gather(
        api.user_tweets_and_replies(user_id, limit=FETCH_LIMIT)
    )

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=FRESH_MINUTES)

    fresh_recent = []
    fetched_ids = []

    for tweet in tweets:
        username = (
            getattr(getattr(tweet, "user", None), "username", "")
            or USERNAME
        )

        if username.lower() != USERNAME.lower():
            continue

        tweet_id = str(tweet.id)
        item_id = f"{SOURCE_KEY}:{tweet_id}"
        fetched_ids.append(item_id)

        if item_id in seen:
            continue

        if snowflake_datetime(tweet_id) >= cutoff:
            fresh_recent.append(tweet)

    # Old unseen posts are silently baselined so the first repair run
    # does not flood ntfy with days of old KAE posts.
    seen.update(fetched_ids)

    for tweet in sorted(
        fresh_recent,
        key=lambda t: int(t.id),
    ):
        notify(tweet)
        print(
            "OFFICIAL KAE X DIRECT SENT:",
            f"https://x.com/{USERNAME}/status/{tweet.id}",
        )

    state["ids"] = sorted(seen)
    save_state(state)

    print(
        f"Official KAE X direct: {len(tweets)} fetched, "
        f"{len(fresh_recent)} recent unseen sent"
    )


if __name__ == "__main__":
    asyncio.run(main())
