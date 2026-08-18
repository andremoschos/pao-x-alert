import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

import requests

STATE = Path("youtube_seen.json")
MAX_SEEN = 5000
TOPIC = os.environ["NTFY_YOUTUBE_TOPIC"]
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()

# Official channels: Panathinaikos FC and Panathinaikos BC.
OFFICIAL_CHANNELS = [
    ("PAE", "UCvDGYaeFq9sBdj0cGnZ_Uhg"),
    ("KAE", "UCbGAOY8tnarNw6T0pHghleg"),
]

# One OR query, so the broad search uses one API search call.
QUERY = "panathinaikos|παναθηναϊκός|παναθηναϊκού|παναθηναϊκό|παναθηναικος|παναθηναικου|παναθηναικο"
SEARCH_EVERY_MINUTES = 0

def now():
    return datetime.now(timezone.utc)

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return {
            "initialized": bool(data.get("initialized", False)),
            "ids": set(str(x) for x in data.get("ids", [])),
            "last_search": data.get("last_search"),
        }
    except Exception:
        return {"initialized": False, "ids": set(), "last_search": None}

def save_state(ids, last_search):
    STATE.write_text(
        json.dumps(
            {
                "initialized": True,
                "ids": sorted(set(ids))[-MAX_SEEN:],
                "last_search": last_search,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

def notify(title, channel, url, official=False):
    endpoint = f"https://ntfy.sh/{TOPIC}"
    headers = {
        "Title": "YOUTUBE OFFICIAL PAO" if official else "YOUTUBE: PANATHINAIKOS",
        "Priority": "high" if official else "default",
        "Tags": "tv",
        "Click": url,
    }
    body = f"{title}\n{channel}\n{url}"
    r = requests.post(endpoint, data=body.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()

def fetch_official_feed(label, channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()

    root = ET.fromstring(r.text)
    ns = {
        "a": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }

    out = []
    for e in root.findall("a:entry", ns):
        vid = (e.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
        channel = (e.findtext("a:author/a:name", default="", namespaces=ns) or "").strip()
        if not vid:
            continue
        out.append({
            "id": f"official:{vid}",
            "title": title or "(χωρίς τίτλο)",
            "channel": channel or label,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "official": True,
        })

    print(f"YouTube official {label}: {len(out)}")
    return out

def should_run_search(last_search):
    last = parse_dt(last_search)
    return last is None or now() - last >= timedelta(minutes=SEARCH_EVERY_MINUTES)

def fetch_broad_search():
    if not API_KEY:
        print("YOUTUBE_API_KEY missing; broad YouTube search skipped.")
        return []

    published_after = (now() - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    params = {
        "part": "snippet",
        "q": QUERY,
        "type": "video",
        "order": "date",
        "maxResults": 50,
        "publishedAfter": published_after,
        "key": API_KEY,
    }

    r = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params=params,
        timeout=30,
    )
    r.raise_for_status()

    out = []
    for item in r.json().get("items", []):
        vid = item.get("id", {}).get("videoId")
        sn = item.get("snippet", {})
        if not vid:
            continue
        out.append({
            "id": f"search:{vid}",
            "title": sn.get("title") or "(χωρίς τίτλο)",
            "channel": sn.get("channelTitle") or "",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "official": False,
        })

    print(f"YouTube broad search results: {len(out)}")
    return out

def main():
    state = load_state()
    seen = state["ids"]
    items = []
    last_search = state["last_search"]

    # Official channel uploads: checked every workflow run, no API quota.
    for label, channel_id in OFFICIAL_CHANNELS:
        try:
            items.extend(fetch_official_feed(label, channel_id))
        except Exception as exc:
            print(f"YouTube official {label} error: {exc}")

    # Broad keyword search: every 15 minutes to stay within free search quota.
    ran_search = False
    if should_run_search(last_search):
        ran_search = True
        try:
            items.extend(fetch_broad_search())
            last_search = now().isoformat()
        except Exception as exc:
            print(f"YouTube broad search error: {exc}")

    dedup = {}
    for x in items:
        # Prefer official labeling when the same video appears in both feeds/search.
        video_url = x["url"]
        if video_url not in dedup or x["official"]:
            dedup[video_url] = x

    unique = list(dedup.values())
    print(f"YouTube unique results: {len(unique)}")

    if not unique:
        save_state(seen, last_search)
        print("No YouTube items right now.")
        return

    if not state["initialized"]:
        seen.update(x["id"] for x in unique)
        save_state(seen, last_search)
        print(f"YouTube baseline saved: {len(unique)}")
        return

    fresh = [x for x in unique if x["id"] not in seen]

    for x in reversed(fresh):
        notify(x["title"], x["channel"], x["url"], x["official"])
        seen.add(x["id"])
        print("YouTube sent:", x["url"])

    seen.update(x["id"] for x in unique)
    save_state(seen, last_search)

    print(f"Fresh YouTube items: {len(fresh)}")
    if not ran_search:
        print("Broad YouTube search not due yet; official channels still checked.")

if __name__ == "__main__":
    main()
