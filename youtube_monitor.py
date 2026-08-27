import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests

STATE = Path("youtube_seen.json")
MAX_SEEN = 5000
MAX_NTFY_BODY_BYTES = 3500

TOPIC = os.environ["NTFY_YOUTUBE_TOPIC"]
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
ATHENS = ZoneInfo("Europe/Athens")

OFFICIAL_CHANNELS = [
    ("PAE", "UCvDGYaeFq9sBdj0cGnZ_Uhg"),
    ("KAE", "UCbGAOY8tnarNw6T0pHghleg"),
]
OFFICIAL_EVERY_MINUTES = 15

QUERY = (
    "panathinaikos|"
    "panathinaikso|"
    "παναθηναϊκός|"
    "παναθηναϊκού|"
    "παναθηναϊκό|"
    "παναθηναικος|"
    "παναθηναικου|"
    "παναθηναικο"
)
DAY_SEARCHES = 93
NIGHT_SEARCH_HOURS = [1, 2, 3, 4, 5, 6, 7]
NIGHT_SEARCH_MINUTE = 34


def now():
    return datetime.now(timezone.utc)


def now_athens():
    return datetime.now(ATHENS)


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return {
            "initialized": bool(data.get("initialized", False)),
            "ids": set(str(x) for x in data.get("ids", [])),
            "last_search": data.get("last_search"),
            "last_official_check": data.get("last_official_check"),
        }
    except Exception:
        return {
            "initialized": False,
            "ids": set(),
            "last_search": None,
            "last_official_check": None,
        }


def save_state(ids, last_search, last_official_check):
    STATE.write_text(
        json.dumps(
            {
                "initialized": True,
                "ids": sorted(set(ids))[-MAX_SEEN:],
                "last_search": last_search,
                "last_official_check": last_official_check,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _format_item(item):
    title = item["title"]
    if len(title) > 600:
        title = title[:597] + "..."
    return f'{title}\n{item["channel"]}\n{item["url"]}'


def _item_batches(items):
    batches = []
    current = []
    for item in items:
        candidate = current + [item]
        body = "\n\n---\n\n".join(_format_item(x) for x in candidate)
        if current and len(body.encode("utf-8")) > MAX_NTFY_BODY_BYTES:
            batches.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def notify_batch(items, official=False):
    endpoint = f"https://ntfy.sh/{TOPIC}"
    count = len(items)
    if official:
        title = "YOUTUBE OFFICIAL PAO" if count == 1 else f"YOUTUBE OFFICIAL PAO - {count} NEW VIDEOS"
        priority = "high"
    else:
        title = "YOUTUBE: PANATHINAIKOS" if count == 1 else f"YOUTUBE: PANATHINAIKOS - {count} NEW VIDEOS"
        priority = "default"

    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": "tv",
        "Click": items[-1]["url"],
    }
    body = "\n\n---\n\n".join(_format_item(item) for item in items)
    r = requests.post(
        endpoint,
        data=body.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
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
    for entry in root.findall("a:entry", ns):
        vid = (entry.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        if not vid:
            continue
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        channel = (entry.findtext("a:author/a:name", default="", namespaces=ns) or "").strip()
        out.append(
            {
                "id": f"official:{vid}",
                "title": title or "(χωρίς τίτλο)",
                "channel": channel or label,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "official": True,
            }
        )
    print(f"YouTube official {label}: {len(out)}")
    return out


def should_run_official(last_official_check):
    last = parse_dt(last_official_check)
    if last is None:
        return True
    return now() - last >= timedelta(minutes=OFFICIAL_EVERY_MINUTES)


def get_cycle_start(local_now):
    if local_now.hour >= 8:
        return local_now.date()
    return local_now.date() - timedelta(days=1)


def build_broad_search_slots(local_now):
    cycle_date = get_cycle_start(local_now)
    start = datetime(
        cycle_date.year,
        cycle_date.month,
        cycle_date.day,
        8,
        4,
        tzinfo=ATHENS,
    )
    next_date = cycle_date + timedelta(days=1)
    end = datetime(
        next_date.year,
        next_date.month,
        next_date.day,
        0,
        59,
        tzinfo=ATHENS,
    )

    all_day_slots = []
    current = start
    while current <= end:
        all_day_slots.append(current)
        current += timedelta(minutes=5)

    chosen_day_slots = []
    total_possible = len(all_day_slots)
    for i in range(DAY_SEARCHES):
        if DAY_SEARCHES == 1:
            index = 0
        else:
            index = round(i * (total_possible - 1) / (DAY_SEARCHES - 1))
        chosen_day_slots.append(all_day_slots[index])

    night_slots = [
        datetime(
            next_date.year,
            next_date.month,
            next_date.day,
            hour,
            NIGHT_SEARCH_MINUTE,
            tzinfo=ATHENS,
        )
        for hour in NIGHT_SEARCH_HOURS
    ]
    slots = chosen_day_slots + night_slots
    slots.sort()
    return slots


def get_latest_due_broad_slot():
    local_now = now_athens()
    due = [slot for slot in build_broad_search_slots(local_now) if slot <= local_now]
    return due[-1] if due else None


def should_run_broad_search(last_search):
    latest_slot = get_latest_due_broad_slot()
    if latest_slot is None:
        return False
    last = parse_dt(last_search)
    if last is None:
        return True
    return latest_slot.astimezone(timezone.utc) > last.astimezone(timezone.utc)


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
        if not vid:
            continue
        snippet = item.get("snippet", {})
        out.append(
            {
                "id": f"search:{vid}",
                "title": snippet.get("title") or "(χωρίς τίτλο)",
                "channel": snippet.get("channelTitle") or "",
                "url": f"https://www.youtube.com/watch?v={vid}",
                "official": False,
            }
        )
    print(f"YouTube broad search results: {len(out)}")
    return out


def main():
    state = load_state()
    seen = state["ids"]
    items = []
    last_search = state["last_search"]
    last_official_check = state["last_official_check"]

    ran_official = False
    if should_run_official(last_official_check):
        ran_official = True
        for label, channel_id in OFFICIAL_CHANNELS:
            try:
                items.extend(fetch_official_feed(label, channel_id))
            except Exception as exc:
                print(f"YouTube official {label} error: {exc}")
        last_official_check = now().isoformat()
    else:
        print("Official PAE/KAE check not due yet.")

    ran_search = False
    if should_run_broad_search(last_search):
        ran_search = True
        try:
            items.extend(fetch_broad_search())
            last_search = now().isoformat()
            local_time = now_athens().strftime("%Y-%m-%d %H:%M:%S")
            print(f"Broad YouTube search executed at Athens time: {local_time}")
        except Exception as exc:
            print(f"YouTube broad search error: {exc}")
    else:
        print("Broad YouTube search not due yet.")

    dedup = {}
    for item in items:
        video_url = item["url"]
        if video_url not in dedup or item["official"]:
            dedup[video_url] = item
    unique = list(dedup.values())
    print(f"YouTube unique results: {len(unique)}")

    if not unique:
        save_state(seen, last_search, last_official_check)
        print("No YouTube items fetched this run.")
        return

    if not state["initialized"]:
        seen.update(item["id"] for item in unique)
        save_state(seen, last_search, last_official_check)
        print(f"YouTube baseline saved: {len(unique)}")
        return

    fresh = [item for item in unique if item["id"] not in seen]
    ordered_fresh = list(reversed(fresh))
    official_fresh = [item for item in ordered_fresh if item["official"]]
    broad_fresh = [item for item in ordered_fresh if not item["official"]]

    for group, is_official in ((official_fresh, True), (broad_fresh, False)):
        for batch in _item_batches(group):
            notify_batch(batch, official=is_official)
            seen.update(item["id"] for item in batch)
            # Persist each successful batch immediately. If a later batch hits
            # ntfy quota, already delivered videos will not be duplicated.
            save_state(seen, last_search, last_official_check)
            for item in batch:
                print("YouTube sent:", item["url"])
            print(f"YouTube ntfy batch sent: {len(batch)} videos")

    seen.update(item["id"] for item in unique)
    save_state(seen, last_search, last_official_check)
    print(f"Fresh YouTube items: {len(fresh)}")

    if not ran_search:
        print("Broad search skipped on this workflow run.")
    if not ran_official:
        print("Official PAE/KAE feeds skipped on this workflow run.")


if __name__ == "__main__":
    main()
