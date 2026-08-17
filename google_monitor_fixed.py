import os
import json
import html
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import xml.etree.ElementTree as ET
import requests

STATE = Path("google_seen.json")
MAX_SEEN = 3000
RSS_URL = os.environ["GOOGLE_ALERT_RSS"]
TOPIC = os.environ["NTFY_TOPIC"]

def load_seen():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return set(str(x) for x in data.get("ids", [])), bool(data.get("initialized", False))
    except Exception:
        return set(), False

def save_seen(ids, initialized=True):
    ordered = sorted(set(str(x) for x in ids))
    STATE.write_text(
        json.dumps(
            {"initialized": initialized, "ids": ordered[-MAX_SEEN:]},
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8",
    )

def clean_text(s):
    if not s:
        return ""
    import re
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

def parse_entries(xml_text):
    root = ET.fromstring(xml_text)
    entries = []

    atom_ns = {"a": "http://www.w3.org/2005/Atom"}
    atom_entries = root.findall(".//a:entry", atom_ns)

    if atom_entries:
        for e in atom_entries:
            eid = (e.findtext("a:id", default="", namespaces=atom_ns) or "").strip()
            title = clean_text(e.findtext("a:title", default="", namespaces=atom_ns))
            link_el = e.find("a:link", atom_ns)
            link = link_el.get("href", "") if link_el is not None else ""
            updated = (e.findtext("a:updated", default="", namespaces=atom_ns) or "").strip()
            key = eid or link or f"{title}|{updated}"
            entries.append({
                "id": key,
                "title": title or "(χωρίς τίτλο)",
                "url": unwrap_google_url(link),
            })
        return entries

    for item in root.findall(".//item"):
        guid = (item.findtext("guid") or "").strip()
        title = clean_text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        key = guid or link or f"{title}|{pub}"
        entries.append({
            "id": key,
            "title": title or "(χωρίς τίτλο)",
            "url": unwrap_google_url(link),
        })

    return entries

def notify(entry):
    endpoint = f"https://ntfy.sh/{TOPIC}"
    headers = {
        "Title": "NEO GOOGLE: PANATHINAIKOS",
        "Priority": "high",
        "Tags": "mag",
    }
    if entry["url"]:
        headers["Click"] = entry["url"]

    body = entry["title"] + (f"\n{entry['url']}" if entry["url"] else "")
    r = requests.post(
        endpoint,
        data=body.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()

def main():
    seen, initialized = load_seen()

    r = requests.get(
        RSS_URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    r.raise_for_status()

    entries = parse_entries(r.text)
    print(f"Google Alert results: {len(entries)}")

    # A brand-new Google Alert feed can legitimately be empty.
    # Treat that as a successful no-op and wait for the first result.
    if not entries:
        print("Google Alert feed is empty for now; waiting for first result.")
        return

    if not initialized:
        seen.update(e["id"] for e in entries)
        save_seen(seen, initialized=True)
        print(f"Google baseline saved: {len(entries)} entries")
        return

    fresh = [e for e in entries if e["id"] not in seen]

    for e in reversed(fresh):
        notify(e)
        seen.add(e["id"])
        print("Google sent:", e["url"] or e["title"])

    seen.update(e["id"] for e in entries)
    save_seen(seen, initialized=True)
    print(f"Fresh Google results: {len(fresh)}")

if __name__ == "__main__":
    main()
