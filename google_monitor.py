import os
import json
import html
import hashlib
import re
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs
import xml.etree.ElementTree as ET

import requests

STATE = Path("google_seen.json")
ENGINE = "google_global_news_plus_web_v2"
MAX_SEEN = 12000

ALERT_RSS = os.environ["GOOGLE_ALERT_RSS"]
TOPIC = os.environ["NTFY_GOOGLE_TOPIC"]

# Greek + Latin spellings that commonly appear internationally.
TERMS = [
    "panathinaikos",
    "Panathinaïkos",
    "παναθηναϊκός",
    "παναθηναϊκού",
    "παναθηναϊκό",
    "παναθηναικος",
    "παναθηναικου",
    "παναθηναικο",
]

# Multiple Google News editions to widen international coverage.
# This does not mean literally every country indexed by Google,
# but it greatly broadens coverage beyond the Greek edition.
NEWS_EDITIONS = [
    ("GR", "el", "el"),       # Greece
    ("US", "en-US", "en"),    # USA
    ("GB", "en-GB", "en"),    # United Kingdom
    ("ES", "es", "es"),       # Spain
    ("IT", "it", "it"),       # Italy
    ("FR", "fr", "fr"),       # France
    ("DE", "de", "de"),       # Germany
    ("TR", "tr", "tr"),       # Turkey
    ("NL", "nl", "nl"),       # Netherlands
    ("PL", "pl", "pl"),       # Poland
    ("RO", "ro", "ro"),       # Romania
    ("CZ", "cs", "cs"),       # Czechia
    ("RS", "sr", "sr"),       # Serbia
    ("HR", "hr", "hr"),       # Croatia
    ("AU", "en-AU", "en"),    # Australia
    ("CA", "en-CA", "en"),    # Canada
    ("IN", "en-IN", "en"),    # India
]

QUERY = "(" + " OR ".join(f'"{t}"' for t in TERMS) + ") when:1d"


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return {
            "engine": data.get("engine"),
            "ids": set(str(x) for x in data.get("ids", [])),
        }
    except Exception:
        return {"engine": None, "ids": set()}


def save_state(ids):
    ordered = sorted(set(str(x) for x in ids))
    STATE.write_text(
        json.dumps(
            {
                "engine": ENGINE,
                "initialized": True,
                "ids": ordered[-MAX_SEEN:],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def clean_text(s):
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return " ".join(s.split())


def unwrap_google_url(url):
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        for key in ("url", "q"):
            if key in qs and qs[key]:
                return qs[key][0]
    except Exception:
        pass
    return url


def canonical_title(title):
    t = clean_text(title).lower()
    # Google News often appends " - Publisher".
    t = re.sub(r"\s+-\s+[^-]{1,100}$", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def make_key(title, url, fallback=""):
    basis = canonical_title(title) or unwrap_google_url(url) or fallback
    return hashlib.sha256(basis.encode("utf-8", errors="ignore")).hexdigest()


def parse_google_alert_atom(xml_text):
    root = ET.fromstring(xml_text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = []

    for e in root.findall(".//a:entry", ns):
        eid = (e.findtext("a:id", default="", namespaces=ns) or "").strip()
        title = clean_text(e.findtext("a:title", default="", namespaces=ns))
        link_el = e.find("a:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        updated = (e.findtext("a:updated", default="", namespaces=ns) or "").strip()
        url = unwrap_google_url(link)

        entries.append({
            "id": make_key(title, url, eid or updated),
            "title": title or "(χωρίς τίτλο)",
            "url": url,
            "source": "WEB",
            "publisher": "",
            "edition": "Google Alerts / Web",
        })

    return entries


def fetch_web_alert():
    r = requests.get(
        ALERT_RSS,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    r.raise_for_status()

    entries = parse_google_alert_atom(r.text)
    print(f"Google Web/Alerts results: {len(entries)}")
    return entries


def fetch_google_news_edition(country, hl, lang):
    q = quote(QUERY)
    url = (
        f"https://news.google.com/rss/search?q={q}"
        f"&hl={quote(hl)}&gl={country}&ceid={country}:{lang}"
    )

    r = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    r.raise_for_status()

    root = ET.fromstring(r.text)
    entries = []

    for item in root.findall(".//item"):
        title = clean_text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        pubdate = (item.findtext("pubDate") or "").strip()
        publisher = clean_text(item.findtext("source") or "")

        entries.append({
            "id": make_key(title, link, guid or pubdate),
            "title": title or "(χωρίς τίτλο)",
            "url": link,
            "source": "NEWS",
            "publisher": publisher,
            "edition": country,
        })

    print(f"Google News [{country}]: {len(entries)}")
    return entries


def notify(entry):
    endpoint = f"https://ntfy.sh/{TOPIC}"

    if entry["source"] == "NEWS":
        alert_title = "NEO GOOGLE NEWS: PANATHINAIKOS"
        tags = "newspaper"
    else:
        alert_title = "NEO GOOGLE WEB: PANATHINAIKOS"
        tags = "mag"

    headers = {
        "Title": alert_title,
        "Priority": "high",
        "Tags": tags,
    }

    if entry["url"]:
        headers["Click"] = entry["url"]

    body = entry["title"]

    if entry.get("publisher"):
        body += f"\nΠηγή: {entry['publisher']}"

    if entry["source"] == "NEWS":
        body += f"\nGoogle News edition: {entry['edition']}"

    if entry["url"]:
        body += f"\n{entry['url']}"

    r = requests.post(
        endpoint,
        data=body.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()


def main():
    state = load_state()
    seen = state["ids"]

    merged = {}

    # 1) Broad Web monitoring via Google Alerts
    try:
        for e in fetch_web_alert():
            merged[e["id"]] = e
    except Exception as exc:
        print(f"Google Web/Alerts error: {exc}")

    # 2) International Google News editions
    successful_editions = 0

    for country, hl, lang in NEWS_EDITIONS:
        try:
            entries = fetch_google_news_edition(country, hl, lang)
            successful_editions += 1

            for e in entries:
                # Prefer NEWS data if the same story is also in Web alert.
                merged[e["id"]] = e

        except Exception as exc:
            # One regional edition failing should not stop all notifications.
            print(f"Google News error [{country}]: {exc}")

    print(f"Successful Google News editions: {successful_editions}/{len(NEWS_EDITIONS)}")
    print(f"Combined unique Google results: {len(merged)}")

    entries = list(merged.values())

    if not entries:
        print("No Google results right now.")
        return

    # Baseline after installing this global version:
    # avoids flooding ntfy with already-existing stories.
    if state["engine"] != ENGINE:
        seen.update(e["id"] for e in entries)
        save_state(seen)
        print(f"GLOBAL Google baseline saved: {len(entries)} entries")
        return

    fresh = [e for e in entries if e["id"] not in seen]

    for e in reversed(fresh):
        notify(e)
        seen.add(e["id"])
        print(f"Google {e['source']} sent:", e["title"])

    seen.update(e["id"] for e in entries)
    save_state(seen)

    print(f"Fresh GLOBAL Google results: {len(fresh)}")


if __name__ == "__main__":
    main()
