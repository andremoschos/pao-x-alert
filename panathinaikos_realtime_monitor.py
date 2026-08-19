import os
import json
import re
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from twscrape import API, gather

STATE = Path("panathinaikos_seen.json")
QUERY = "panathinaikos"
POLL_SECONDS = 60
FETCH_LIMIT = 100
FRESH_MINUTES = 5
MAX_SEEN = 5000

AUTH = os.environ["X_AUTH_TOKEN"]
CT0 = os.environ["X_CT0"]
TOPIC = os.environ["NTFY_PANATHINAIKOS_TOPIC"]

STATUS_RE = re.compile(r"/status/(\d+)")


def load_local_seen():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return {str(x) for x in data.get("ids", [])}
    except Exception:
        return set()


def load_recent_ntfy_seen():
    seen = set()
    try:
        r = requests.get(
            f"https://ntfy.sh/{TOPIC}/json",
            params={"poll": "1", "since": "3h"},
            timeout=25,
        )
        r.raise_for_status()

        for line in r.text.splitlines():
            if not line.strip():
                continue

            try:
                msg = json.loads(line)
            except Exception:
                continue

            if msg.get("event") != "message":
                continue

            haystack = " ".join(
                str(msg.get(k, "") or "")
                for k in ("click", "message", "title")
            )

            for match in STATUS_RE.findall(haystack):
                seen.add(match)

    except Exception as exc:
        print(f"ntfy history seed warning: {exc}")

    return seen


def save_local_seen(seen):
    ordered = sorted(
        {str(x) for x in seen if str(x).isdigit()},
        key=int,
    )[-MAX_SEEN:]

    STATE.write_text(
        json.dumps(
            {
                "engine": "twscrape_panathinaikos_realtime_v1",
                "initialized": True,
                "ids": ordered,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def snowflake_datetime(tweet_id):
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
    body = (
        f'{tweet["author"]}\n'
        f'{tweet["text"]}\n'
        f'{tweet["url"]}'
    )

    r = requests.post(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()


async def make_api():
    db = "/tmp/twscrape_panathinaikos_realtime.db"
    Path(db).unlink(missing_ok=True)

    api = API(
        db,
        raise_when_no_account=True,
        wait_timeout=30,
        wait_interval=1,
    )

    cookie_header = f"auth_token={AUTH}; ct0={CT0}"
    await api.pool.add_account_cookies(
        "newspao_panathinaikos_realtime",
        cookie_header,
    )

    return api


async def fetch_latest(api):
    results = await gather(api.search(QUERY, limit=FETCH_LIMIT))

    tweets = []
    seen_now = set()

    for t in results:
        tid = str(t.id)

        if tid in seen_now:
            continue
        seen_now.add(tid)

        username = (
            getattr(getattr(t, "user", None), "username", "")
            or "unknown"
        )
        text = (
            getattr(t, "rawContent", "")
            or ""
        ).strip() or "(post without text)"

        tweets.append(
            {
                "id": tid,
                "author": f"@{username}",
                "text": text,
                "url": f"https://x.com/{username}/status/{tid}",
                "created": snowflake_datetime(tid),
            }
        )

    return tweets


async def main():
    seen = load_local_seen()
    ntfy_seen = load_recent_ntfy_seen()
    seen.update(ntfy_seen)

    print(
        f"Realtime watcher starting. "
        f"local+ntfy seen={len(seen)}, poll={POLL_SECONDS}s"
    )

    api = await make_api()

    while True:
        started = datetime.now(timezone.utc)

        try:
            tweets = await fetch_latest(api)
            cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=FRESH_MINUTES
            )

            fresh = [
                t
                for t in tweets
                if t["id"] not in seen
                and t["created"] >= cutoff
            ]
            fresh.sort(key=lambda x: x["created"])

            for tweet in fresh:
                notify(tweet)
                seen.add(tweet["id"])
                print("REALTIME SENT:", tweet["url"])

            # Everything returned by search becomes known locally.
            # Older unseen posts are silently baselined and never flood ntfy.
            seen.update(t["id"] for t in tweets)
            save_local_seen(seen)

            print(
                f"{started.isoformat()} | "
                f"results={len(tweets)} fresh={len(fresh)}"
            )

        except Exception as exc:
            print(f"Realtime poll error: {type(exc).__name__}: {exc}")

        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
