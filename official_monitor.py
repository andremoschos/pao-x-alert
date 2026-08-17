import os
import json
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

STATE = Path("official_seen.json")
MAX_SEEN = 2000
TOPIC = os.environ["NTFY_OFFICIAL_TOPIC"]

SOURCES = [
    ("PAE", "https://www.pao.gr/all-news/"),
    ("KAE", "https://www.paobc.gr/en/news/"),
]

class H3LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_h3 = 0
        self.href = None
        self.parts = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag == "h3":
            self.in_h3 += 1
            self.href = None
            self.parts = []
        elif self.in_h3 and tag == "a":
            attrs = dict(attrs)
            if attrs.get("href"):
                self.href = attrs["href"]

    def handle_data(self, data):
        if self.in_h3:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "h3" and self.in_h3:
            title = " ".join("".join(self.parts).split())
            if self.href and title:
                self.items.append((title, self.href))
            self.in_h3 -= 1
            self.href = None
            self.parts = []

def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return set(data.get("ids", [])), bool(data.get("initialized", False))
    except Exception:
        return set(), False

def save_state(ids):
    STATE.write_text(
        json.dumps(
            {"initialized": True, "ids": sorted(set(ids))[-MAX_SEEN:]},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

def fetch_source(label, url):
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()

    parser = H3LinkParser()
    parser.feed(r.text)

    base_host = urlparse(url).netloc
    out = []
    seen_urls = set()

    for title, href in parser.items:
        full = urljoin(url, href)
        if urlparse(full).netloc != base_host:
            continue
        if full in seen_urls:
            continue
        seen_urls.add(full)
        out.append({
            "id": full,
            "title": title,
            "url": full,
            "label": label,
        })

    print(f"{label} official results: {len(out)}")
    return out

def notify(item):
    endpoint = f"https://ntfy.sh/{TOPIC}"
    title = "OFFICIAL PAO - PAE" if item["label"] == "PAE" else "OFFICIAL PAO - KAE"
    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "green_circle",
        "Click": item["url"],
    }
    body = f'{item["title"]}\n{item["url"]}'
    r = requests.post(endpoint, data=body.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()

def main():
    seen, initialized = load_state()

    merged = {}
    for label, url in SOURCES:
        try:
            for item in fetch_source(label, url):
                merged[item["id"]] = item
        except Exception as exc:
            print(f"{label} error: {exc}")

    items = list(merged.values())
    print(f"Official PAO unique results: {len(items)}")

    if not items:
        print("No official PAO items right now.")
        return

    if not initialized:
        seen.update(x["id"] for x in items)
        save_state(seen)
        print(f"Official PAO baseline saved: {len(items)}")
        return

    fresh = [x for x in items if x["id"] not in seen]

    for item in reversed(fresh):
        notify(item)
        seen.add(item["id"])
        print("Official PAO sent:", item["url"])

    seen.update(x["id"] for x in items)
    save_state(seen)
    print(f"Fresh official PAO items: {len(fresh)}")

if __name__ == "__main__":
    main()
