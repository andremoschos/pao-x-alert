import os
import json
import re
import asyncio
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from twscrape import API, gather


QUERY = "panathinaikos"

# SearchTimeline is explicitly Latest in twscrape.
# Use two compact pages every 2 minutes instead of one page every minute:
# roughly the same average X request pressure, but twice the depth per cycle.
FETCH_LIMIT = 40
POLL_SECONDS = 120

# X search/indexing can surface a post several minutes after it was actually
# created. The old 4-minute window silently lost those delayed results.
# Keep a 2-hour recovery window; ntfy history + local IDs still prevent dupes.
FRESH_MINUTES = 120
MAX_SEEN = 5000

STATE = Path("panathinaikos_seen.json")

AUTH = os.environ["X_AUTH_TOKEN"]
CT0 = os.environ["X_CT0"]
TOPIC = os.environ["NTFY_PANATHINAIKOS_TOPIC"]

STATUS_RE = re.compile(r"/status/(\d+)")


def snowflake_datetime(tweet_id):
    ms = (int(tweet_id) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def load_local_seen():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return {str(x) for x in data.get("ids", [])}
    except Exception:
        return set()


def load_ntfy_seen():
    seen = set()

    try:
        r = requests.get(
            f"https://ntfy.sh/{TOPIC}/json",
            params={"poll": "1", "since": "12h"},
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

            for tweet_id in STATUS_RE.findall(haystack):
                seen.add(tweet_id)

    except Exception as exc:
        print(
            f"ntfy seed warning: {type(exc).__name__}: {exc}",
            flush=True,
        )

    return seen


def notify(tweet):
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
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()


async def make_api():
    db = "/tmp/twscrape_panathinaikos_realtime_v2.db"
    Path(db).unlink(missing_ok=True)

    api = API(
        db,
        raise_when_no_account=True,
        wait_timeout=10,
        wait_interval=1,
    )

    cookie_header = f"auth_token={AUTH}; ct0={CT0}"

    await api.pool.add_account_cookies(
        "newspao_panathinaikos_realtime_v2",
        cookie_header,
    )

    return api


async def fetch_latest(api):
    # twscrape SearchTimeline defaults to product="Latest".
    # limit=40 normally means about two compact pages.
    results = await gather(
        api.search(QUERY, limit=FETCH_LIMIT)
    )

    tweets = []
    ids = set()

    for t in results:
        tid = str(t.id)

        if tid in ids:
            continue
        ids.add(tid)

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


def remember(seen, order, tweet_id):
    if tweet_id in seen:
        return

    seen.add(tweet_id)
    order.append(tweet_id)

    while len(order) > MAX_SEEN:
        old = order.popleft()
        seen.discard(old)


async def main():
    seen = load_local_seen()
    seen.update(load_ntfy_seen())
    order = deque(seen)

    print(
        f"X REALTIME V2 starting | "
        f"query={QUERY!r} | "
        f"poll={POLL_SECONDS}s | "
        f"fetch_limit={FETCH_LIMIT} | "
        f"recovery={FRESH_MINUTES}m | "
        f"seen={len(seen)}",
        flush=True,
    )

    api = await make_api()

    while True:
        cycle_started = datetime.now(timezone.utc)

        try:
            tweets = await fetch_latest(api)

            cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=FRESH_MINUTES
            )

            # Anything inside the recovery window is still eligible even if X
            # only exposed/indexed it late. Already delivered IDs are excluded
            # by local state + ntfy history.
            fresh = [
                t for t in tweets
                if t["id"] not in seen
                and t["created"] >= cutoff
            ]
            fresh.sort(key=lambda x: x["created"])

            sent = 0

            for tweet in fresh:
                try:
                    notify(tweet)
                except Exception as exc:
                    print(
                        f"NTFY SEND FAIL {tweet['url']}: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    # Do NOT mark it seen. Retry next cycle.
                    continue

                remember(seen, order, tweet["id"])
                sent += 1

                print(
                    "X REALTIME SENT:",
                    tweet["url"],
                    flush=True,
                )

            # Only results older than the generous recovery window are silently
            # baselined. This prevents ancient-post floods without losing posts
            # that X surfaced late.
            for tweet in tweets:
                if (
                    tweet["id"] not in seen
                    and tweet["created"] < cutoff
                ):
                    remember(seen, order, tweet["id"])

            print(
                f"{cycle_started.isoformat()} | "
                f"results={len(tweets)} | "
                f"eligible={len(fresh)} | "
                f"sent={sent}",
                flush=True,
            )

        except Exception as exc:
            print(
                f"X realtime cycle error: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
