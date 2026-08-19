import json
import random
import re
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests


QUERY = "panathinaikos"
POLL_SECONDS = 60
FRESH_MINUTES = 6
MAX_SEEN = 5000
MAX_INSTANCE_ATTEMPTS = 4
INSTANCE_COOLDOWN_SECONDS = 300

TOPIC = __import__("os").environ["NTFY_PANATHINAIKOS_TOPIC"]

# Public Nitter mirrors. The watcher rotates/fails over automatically.
NITTER_INSTANCES = [
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://nitter.privacyredirect.com",
    "https://nitter.tiekoetter.com",
    "https://nitter.space",
    "https://lightbrd.com",
]

STATUS_RE = re.compile(r"/status/(\d+)")
USER_STATUS_RE = re.compile(r"^/([^/]+)/status/(\d+)")

cooldown_until = {}


def snowflake_datetime(tweet_id):
    ms = (int(tweet_id) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def load_ntfy_seen():
    seen = set()

    try:
        r = requests.get(
            f"https://ntfy.sh/{TOPIC}/json",
            params={"poll": "1", "since": "24h"},
            impersonate="chrome",
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
        print(f"ntfy seed warning: {type(exc).__name__}: {exc}", flush=True)

    return seen


def notify(tweet):
    headers = {
        "Title": "X PANATHINAIKOS",
        "Priority": "high",
        "Tags": "mag",
        "Click": tweet["url"],
    }

    body = (
        f'@{tweet["username"]}\n'
        f'{tweet["text"]}\n'
        f'{tweet["url"]}'
    )

    r = requests.post(
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        impersonate="chrome",
        timeout=20,
    )
    r.raise_for_status()


def normalize_status_href(href):
    if not href:
        return None, None

    # Absolute Nitter/X URL -> keep only the path.
    if href.startswith("http://") or href.startswith("https://"):
        href = urlparse(href).path

    # Strip fragments/query.
    href = href.split("#", 1)[0].split("?", 1)[0]

    match = USER_STATUS_RE.match(href)
    if not match:
        return None, None

    username, tweet_id = match.groups()
    return username.lstrip("@"), tweet_id


def parse_nitter(html):
    lower_html = html.lower()

    if (
        "verifying your browser" in lower_html
        or "just a moment" in lower_html
        or "cf-chl-" in lower_html
    ):
        raise RuntimeError("browser challenge")

    soup = BeautifulSoup(html, "html.parser")
    posts = []
    ids = set()

    for item in soup.select(".timeline-item"):
        link = item.select_one("a.tweet-link[href*='/status/']")
        if link is None:
            link = item.select_one(".tweet-date a[href*='/status/']")
        if link is None:
            link = item.select_one("a[href*='/status/']")

        if link is None:
            continue

        username, tweet_id = normalize_status_href(link.get("href", ""))
        if not username or not tweet_id or tweet_id in ids:
            continue

        content = item.select_one(".tweet-content")
        text = content.get_text(" ", strip=True) if content else ""

        # Nitter search occasionally returns unrelated garbage.
        # Enforce the exact keyword locally as a second filter.
        if QUERY.casefold() not in text.casefold():
            continue

        ids.add(tweet_id)

        posts.append(
            {
                "id": tweet_id,
                "username": username,
                "text": text or "(post without text)",
                "url": f"https://x.com/{username}/status/{tweet_id}",
                "created": snowflake_datetime(tweet_id),
            }
        )

    return posts


def fetch_instance(instance):
    r = requests.get(
        f"{instance}/search",
        params={"f": "tweets", "q": QUERY},
        headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
        impersonate="chrome",
        timeout=20,
        allow_redirects=True,
    )
    r.raise_for_status()

    posts = parse_nitter(r.text)

    if not posts:
        raise RuntimeError("0 matching posts returned")

    return posts


def get_search_results():
    now_ts = time.time()

    candidates = [
        x for x in NITTER_INSTANCES
        if cooldown_until.get(x, 0) <= now_ts
    ]

    if not candidates:
        # If every mirror is cooling down, retry the one whose cooldown ends first.
        candidates = sorted(
            NITTER_INSTANCES,
            key=lambda x: cooldown_until.get(x, 0),
        )[:1]
    else:
        random.shuffle(candidates)

    errors = []

    for instance in candidates[:MAX_INSTANCE_ATTEMPTS]:
        try:
            posts = fetch_instance(instance)

            newest = max(p["created"] for p in posts)
            age = datetime.now(timezone.utc) - newest

            # A mirror serving very old results is probably stale.
            # Don't reject quiet periods too aggressively: 12 hours is generous.
            if age > timedelta(hours=12):
                raise RuntimeError(
                    f"stale results; newest is {age} old"
                )

            print(
                f"NITTER OK {instance}: "
                f"{len(posts)} matching results; "
                f"newest={newest.isoformat()}",
                flush=True,
            )
            return posts, instance

        except Exception as exc:
            cooldown_until[instance] = (
                time.time() + INSTANCE_COOLDOWN_SECONDS
            )
            msg = f"{instance}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            print("NITTER FAIL", msg, flush=True)

    raise RuntimeError("All Nitter attempts failed | " + " | ".join(errors))


def trim_seen(seen, order):
    while len(order) > MAX_SEEN:
        old = order.popleft()
        seen.discard(old)


def main():
    seen = load_ntfy_seen()
    order = deque(seen)

    print(
        f"FREE realtime watcher starting | "
        f"query={QUERY!r} | poll={POLL_SECONDS}s | "
        f"ntfy_seen={len(seen)}",
        flush=True,
    )

    while True:
        cycle_started = datetime.now(timezone.utc)

        try:
            posts, instance = get_search_results()
            cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=FRESH_MINUTES
            )

            fresh = [
                p for p in posts
                if p["id"] not in seen
                and p["created"] >= cutoff
            ]
            fresh.sort(key=lambda p: p["created"])

            for post in fresh:
                notify(post)
                seen.add(post["id"])
                order.append(post["id"])
                trim_seen(seen, order)

                print(
                    "FREE REALTIME SENT:",
                    post["url"],
                    flush=True,
                )

            # Silently baseline older results so there is no notification flood.
            for post in posts:
                if post["id"] not in seen:
                    seen.add(post["id"])
                    order.append(post["id"])

            trim_seen(seen, order)

            print(
                f"{cycle_started.isoformat()} | "
                f"source={instance} | "
                f"results={len(posts)} | fresh={len(fresh)}",
                flush=True,
            )

        except Exception as exc:
            print(
                f"FREE realtime cycle error: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
