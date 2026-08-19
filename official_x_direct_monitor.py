import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from twscrape import API, gather


STATE = Path("official_seen.json")
FRESH_MINUTES = 120
FETCH_LIMIT = 40
MAX_SEEN = 10000

TOPIC = os.environ["NTFY_OFFICIAL_TOPIC"]
X_AUTH_TOKEN = os.environ["X_AUTH_TOKEN"]
X_CT0 = os.environ["X_CT0"]

X_SOURCES = [
    {
        "key": "x_pae",
        "org": "PAE",
        "username": "paofc_",
    },
    {
        "key": "x_kae",
        "org": "KAE",
        "username": "Paobcgr",
    },
    {
        "key": "x_ao",
        "org": "AO",
        "username": "acpanathinaikos",
    },
]


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

    initialized = set(
        str(x) for x in data.get("initialized_sources", [])
    )
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
    username = (
        getattr(getattr(tweet, "user", None), "username", "")
        or source["username"]
    )
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


async def fetch_source(api, source):
    user = await api.user_by_login(source["username"])
    if not user:
        raise RuntimeError(
            f"Could not resolve @{source['username']}"
        )

    user_id = getattr(user, "id", None)
    if not user_id:
        raise RuntimeError(
            f"No user id returned for @{source['username']}"
        )

    tweets = await gather(
        api.user_tweets_and_replies(user_id, limit=FETCH_LIMIT)
    )

    out = []
    for tweet in tweets:
        username = (
            getattr(getattr(tweet, "user", None), "username", "")
            or source["username"]
        )

        if username.lower() != source["username"].lower():
            continue

        out.append(tweet)

    return out


async def main():
    state = load_state()
    seen = set(str(x) for x in state.get("ids", []))

    db = "/tmp/twscrape_official_x_direct.db"
    Path(db).unlink(missing_ok=True)

    api = API(
        db,
        raise_when_no_account=True,
        wait_timeout=30,
        wait_interval=1,
    )

    cookie_header = f"auth_token={X_AUTH_TOKEN}; ct0={X_CT0}"
    await api.pool.add_account_cookies(
        "official-x-direct",
        cookie_header,
    )

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=FRESH_MINUTES)

    total_sent = 0

    for source in X_SOURCES:
        try:
            tweets = await fetch_source(api, source)

            fetched_ids = []
            fresh_recent = []

            for tweet in tweets:
                tweet_id = str(tweet.id)
                item_id = f"{source['key']}:{tweet_id}"
                fetched_ids.append(item_id)

                if item_id in seen:
                    continue

                if snowflake_datetime(tweet_id) >= cutoff:
                    fresh_recent.append(tweet)

            # Baseline older unseen items silently so the first run
            # cannot flood ntfy with old posts.
            seen.update(fetched_ids)

            for tweet in sorted(
                fresh_recent,
                key=lambda t: int(t.id),
            ):
                notify(source, tweet)
                total_sent += 1

                print(
                    "OFFICIAL X DIRECT SENT:",
                    source["org"],
                    f"https://x.com/{source['username']}/status/{tweet.id}",
                )

            print(
                f"{source['key']} direct: {len(tweets)} fetched, "
                f"{len(fresh_recent)} recent unseen sent"
            )

        except Exception as exc:
            # One account failing must not kill the whole Official watcher.
            # official_monitor.py still keeps the normal X Search as fallback.
            print(
                f"{source['key']} direct error: {exc}"
            )

    state["ids"] = sorted(seen)
    save_state(state)

    print(
        f"Official X direct complete: {total_sent} notifications sent"
    )


if __name__ == "__main__":
    asyncio.run(main())
