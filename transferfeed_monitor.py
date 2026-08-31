import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import telegram_delivery as telegram


STATE = Path("transferfeed_seen.json")
ENGINE = "transferfeed_panathinaikos_v1"
URL = "https://www.transferfeed.com/clubs/panathinaikos/53"
HOST = "transferfeed.com"
MAX_SEEN = 2000


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = None
        self.parts = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data):
        if self.href:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            text = " ".join("".join(self.parts).split()).strip()
            if text:
                self.items.append((text, self.href))
            self.href = None
            self.parts = []


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return {
            "engine": data.get("engine"),
            "ids": set(map(str, data.get("ids", []))),
        }
    except Exception:
        return {"engine": None, "ids": set()}


def save_state(ids):
    STATE.write_text(
        json.dumps(
            {
                "engine": ENGINE,
                "initialized": True,
                "ids": sorted(set(map(str, ids)))[-MAX_SEEN:],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def canonical_host(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def fetch_items():
    r = requests.get(
        URL,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131 Safari/537.36"
            )
        },
    )
    r.raise_for_status()

    parser = LinkParser()
    parser.feed(r.text)

    out = []
    used = set()

    for text, href in parser.items:
        full = urljoin(URL, href).split("#")[0]
        if canonical_host(full) != HOST:
            continue

        path = urlparse(full).path.rstrip("/")
        if not path.startswith("/transfers/"):
            continue

        if full in used:
            continue
        used.add(full)

        title = text
        # TransferFeed anchor text often includes the age and a short summary.
        # Keep it readable in Telegram without losing the player/context.
        if len(title) > 700:
            title = title[:697].rstrip() + "..."

        out.append(
            {
                "id": full,
                "title": title,
                "url": full,
            }
        )

        if len(out) >= 50:
            break

    print(f"TransferFeed Panathinaikos results: {len(out)}", flush=True)
    return out


def notify(item):
    body = (
        f'🔄 PANATHINAIKOS TRANSFER\n\n'
        f'{item["title"]}\n'
        f'Πηγή: TransferFeed'
    )
    return telegram.send(
        "google_news_web",
        "DIRECT | TRANSFERFEED | PANATHINAIKOS",
        body,
        item["url"],
    )


def main():
    state = load_state()
    seen = state["ids"]
    items = fetch_items()

    if not items:
        raise RuntimeError("TransferFeed Panathinaikos returned no transfer items")

    current_ids = {item["id"] for item in items}

    # First deployment baselines current page so old rumours do not flood Telegram.
    if state["engine"] != ENGINE:
        seen.update(current_ids)
        save_state(seen)
        print(f"TransferFeed baseline saved: {len(items)} items", flush=True)
        return

    fresh = [item for item in items if item["id"] not in seen]

    sent = 0
    for item in reversed(fresh):
        if not notify(item):
            raise RuntimeError("TransferFeed Telegram delivery failed")
        seen.add(item["id"])
        sent += 1
        print(f'TransferFeed sent: {item["url"]}', flush=True)

    seen.update(current_ids)
    save_state(seen)
    print(f"TransferFeed fresh items: {sent}", flush=True)


if __name__ == "__main__":
    main()
