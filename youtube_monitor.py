import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests


STATE = Path("youtube_seen.json")
MAX_SEEN = 5000

TOPIC = os.environ["NTFY_YOUTUBE_TOPIC"]
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()

ATHENS = ZoneInfo("Europe/Athens")


# =========================================================
# OFFICIAL PANATHINAIKOS CHANNELS
# =========================================================

OFFICIAL_CHANNELS = [
    ("PAE", "UCvDGYaeFq9sBdj0cGnZ_Uhg"),
    ("KAE", "UCbGAOY8tnarNw6T0pHghleg"),
]

OFFICIAL_EVERY_MINUTES = 15


# =========================================================
# BROAD YOUTUBE SEARCH
# =========================================================

# One YouTube search request with OR terms.
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

# 93 searches from 08:00 to 01:00
DAY_SEARCHES = 93

# 7 additional searches overnight
NIGHT_SEARCH_HOURS = [
    1,
    2,
    3,
    4,
    5,
    6,
    7,
]

NIGHT_SEARCH_MINUTE = 34


# =========================================================
# TIME HELPERS
# =========================================================

def now():
    return datetime.now(timezone.utc)


def now_athens():
    return datetime.now(ATHENS)


def parse_dt(s):
    if not s:
        return None

    try:
        return datetime.fromisoformat(
            s.replace("Z", "+00:00")
        )

    except Exception:
        return None


# =========================================================
# STATE
# =========================================================

def load_state():
    try:
        data = json.loads(
            STATE.read_text(
                encoding="utf-8"
            )
        )

        return {
            "initialized": bool(
                data.get(
                    "initialized",
                    False,
                )
            ),
            "ids": set(
                str(x)
                for x in data.get(
                    "ids",
                    [],
                )
            ),
            "last_search": data.get(
                "last_search"
            ),
            "last_official_check": data.get(
                "last_official_check"
            ),
        }

    except Exception:
        return {
            "initialized": False,
            "ids": set(),
            "last_search": None,
            "last_official_check": None,
        }


def save_state(
    ids,
    last_search,
    last_official_check,
):
    STATE.write_text(
        json.dumps(
            {
                "initialized": True,
                "ids": sorted(
                    set(ids)
                )[-MAX_SEEN:],
                "last_search": last_search,
                "last_official_check": (
                    last_official_check
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# =========================================================
# NTFY
# =========================================================

def notify(
    title,
    channel,
    url,
    official=False,
):
    endpoint = (
        f"https://ntfy.sh/{TOPIC}"
    )

    headers = {
        "Title": (
            "YOUTUBE OFFICIAL PAO"
            if official
            else "YOUTUBE: PANATHINAIKOS"
        ),
        "Priority": (
            "high"
            if official
            else "default"
        ),
        "Tags": "tv",
        "Click": url,
    }

    body = (
        f"{title}\n"
        f"{channel}\n"
        f"{url}"
    )

    r = requests.post(
        endpoint,
        data=body.encode("utf-8"),
        headers=headers,
        timeout=20,
    )

    r.raise_for_status()


# =========================================================
# OFFICIAL CHANNEL FEEDS
# =========================================================

def fetch_official_feed(
    label,
    channel_id,
):
    url = (
        "https://www.youtube.com/"
        "feeds/videos.xml"
        f"?channel_id={channel_id}"
    )

    r = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    r.raise_for_status()

    root = ET.fromstring(
        r.text
    )

    ns = {
        "a": (
            "http://www.w3.org/"
            "2005/Atom"
        ),
        "yt": (
            "http://www.youtube.com/"
            "xml/schemas/2015"
        ),
    }

    out = []

    for e in root.findall(
        "a:entry",
        ns,
    ):
        vid = (
            e.findtext(
                "yt:videoId",
                default="",
                namespaces=ns,
            )
            or ""
        ).strip()

        title = (
            e.findtext(
                "a:title",
                default="",
                namespaces=ns,
            )
            or ""
        ).strip()

        channel = (
            e.findtext(
                "a:author/a:name",
                default="",
                namespaces=ns,
            )
            or ""
        ).strip()

        if not vid:
            continue

        out.append(
            {
                "id": (
                    f"official:{vid}"
                ),
                "title": (
                    title
                    or "(χωρίς τίτλο)"
                ),
                "channel": (
                    channel
                    or label
                ),
                "url": (
                    "https://www.youtube.com/"
                    f"watch?v={vid}"
                ),
                "official": True,
            }
        )

    print(
        f"YouTube official "
        f"{label}: {len(out)}"
    )

    return out


def should_run_official(
    last_official_check,
):
    last = parse_dt(
        last_official_check
    )

    if last is None:
        return True

    return (
        now() - last
        >= timedelta(
            minutes=OFFICIAL_EVERY_MINUTES
        )
    )


# =========================================================
# BROAD SEARCH SCHEDULE
# =========================================================

def get_cycle_start(local_now):
    """
    Our daily search cycle starts at 08:00 Athens
    and finishes just before 08:00 next day.
    """

    if local_now.hour >= 8:
        return local_now.date()

    return (
        local_now.date()
        - timedelta(days=1)
    )


def build_broad_search_slots(
    local_now,
):
    """
    Creates exactly 100 theoretical broad-search
    slots per Athens 08:00 -> next 08:00 cycle.

    93 slots:
      08:04 -> 00:59

    7 slots:
      01:34 -> 07:34
    """

    cycle_date = get_cycle_start(
        local_now
    )

    start = datetime(
        cycle_date.year,
        cycle_date.month,
        cycle_date.day,
        8,
        4,
        tzinfo=ATHENS,
    )

    next_date = (
        cycle_date
        + timedelta(days=1)
    )

    end = datetime(
        next_date.year,
        next_date.month,
        next_date.day,
        0,
        59,
        tzinfo=ATHENS,
    )

    # GitHub workflow runs on a 5-minute grid:
    # xx:04, xx:09, xx:14 ...
    all_day_slots = []

    current = start

    while current <= end:
        all_day_slots.append(
            current
        )

        current += timedelta(
            minutes=5
        )

    # There are 204 possible 5-minute slots.
    # Select 93 evenly across them.
    chosen_day_slots = []

    total_possible = len(
        all_day_slots
    )

    for i in range(
        DAY_SEARCHES
    ):
        if DAY_SEARCHES == 1:
            index = 0

        else:
            index = round(
                i
                * (
                    total_possible
                    - 1
                )
                / (
                    DAY_SEARCHES
                    - 1
                )
            )

        chosen_day_slots.append(
            all_day_slots[
                index
            ]
        )

    # Seven overnight searches:
    # 01:34, 02:34 ... 07:34
    night_slots = []

    for hour in NIGHT_SEARCH_HOURS:
        night_slots.append(
            datetime(
                next_date.year,
                next_date.month,
                next_date.day,
                hour,
                NIGHT_SEARCH_MINUTE,
                tzinfo=ATHENS,
            )
        )

    slots = (
        chosen_day_slots
        + night_slots
    )

    slots.sort()

    return slots


def get_latest_due_broad_slot():
    local_now = now_athens()

    slots = build_broad_search_slots(
        local_now
    )

    due = [
        slot
        for slot in slots
        if slot <= local_now
    ]

    if not due:
        return None

    return due[-1]


def should_run_broad_search(
    last_search,
):
    latest_slot = (
        get_latest_due_broad_slot()
    )

    if latest_slot is None:
        return False

    last = parse_dt(
        last_search
    )

    if last is None:
        return True

    latest_slot_utc = (
        latest_slot.astimezone(
            timezone.utc
        )
    )

    # If this scheduled slot happened after
    # our previous real API search,
    # one new broad search is due.
    return (
        latest_slot_utc
        > last.astimezone(
            timezone.utc
        )
    )


# =========================================================
# YOUTUBE DATA API BROAD SEARCH
# =========================================================

def fetch_broad_search():
    if not API_KEY:
        print(
            "YOUTUBE_API_KEY missing; "
            "broad YouTube search skipped."
        )

        return []

    published_after = (
        now()
        - timedelta(hours=24)
    ).isoformat().replace(
        "+00:00",
        "Z",
    )

    params = {
        "part": "snippet",
        "q": QUERY,
        "type": "video",
        "order": "date",
        "maxResults": 50,
        "publishedAfter": (
            published_after
        ),
        "key": API_KEY,
    }

    r = requests.get(
        (
            "https://www.googleapis.com/"
            "youtube/v3/search"
        ),
        params=params,
        timeout=30,
    )

    r.raise_for_status()

    out = []

    for item in (
        r.json().get(
            "items",
            [],
        )
    ):
        vid = (
            item.get(
                "id",
                {},
            ).get(
                "videoId"
            )
        )

        sn = item.get(
            "snippet",
            {},
        )

        if not vid:
            continue

        out.append(
            {
                "id": (
                    f"search:{vid}"
                ),
                "title": (
                    sn.get(
                        "title"
                    )
                    or "(χωρίς τίτλο)"
                ),
                "channel": (
                    sn.get(
                        "channelTitle"
                    )
                    or ""
                ),
                "url": (
                    "https://www.youtube.com/"
                    f"watch?v={vid}"
                ),
                "official": False,
            }
        )

    print(
        "YouTube broad search "
        f"results: {len(out)}"
    )

    return out


# =========================================================
# MAIN
# =========================================================

def main():
    state = load_state()

    seen = state["ids"]

    items = []

    last_search = (
        state["last_search"]
    )

    last_official_check = (
        state["last_official_check"]
    )


    # =====================================================
    # OFFICIAL PAE + KAE
    # Every 15 minutes.
    # No YouTube search quota used.
    # =====================================================

    ran_official = False

    if should_run_official(
        last_official_check
    ):
        ran_official = True

        for (
            label,
            channel_id,
        ) in OFFICIAL_CHANNELS:

            try:
                items.extend(
                    fetch_official_feed(
                        label,
                        channel_id,
                    )
                )

            except Exception as exc:
                print(
                    "YouTube official "
                    f"{label} error: "
                    f"{exc}"
                )

        last_official_check = (
            now().isoformat()
        )

    else:
        print(
            "Official PAE/KAE check "
            "not due yet."
        )


    # =====================================================
    # BROAD PANATHINAIKOS SEARCH
    #
    # 93 searches from 08:00 -> 01:00
    # + 7 searches from 01:00 -> 08:00
    # = maximum 100 scheduled searches / cycle.
    # =====================================================

    ran_search = False

    if should_run_broad_search(
        last_search
    ):
        ran_search = True

        try:
            items.extend(
                fetch_broad_search()
            )

            last_search = (
                now().isoformat()
            )

            local_time = (
                now_athens()
                .strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )

            print(
                "Broad YouTube search "
                "executed at Athens time: "
                f"{local_time}"
            )

        except Exception as exc:
            print(
                "YouTube broad search "
                f"error: {exc}"
            )

    else:
        print(
            "Broad YouTube search "
            "not due yet."
        )


    # =====================================================
    # DEDUP
    # =====================================================

    dedup = {}

    for x in items:
        video_url = x["url"]

        # Prefer official labeling when the same
        # video appears both in official feeds
        # and broad search.
        if (
            video_url not in dedup
            or x["official"]
        ):
            dedup[
                video_url
            ] = x

    unique = list(
        dedup.values()
    )

    print(
        "YouTube unique results: "
        f"{len(unique)}"
    )


    # =====================================================
    # NOTHING FETCHED THIS RUN
    # =====================================================

    if not unique:
        save_state(
            seen,
            last_search,
            last_official_check,
        )

        print(
            "No YouTube items "
            "fetched this run."
        )

        return


    # =====================================================
    # FIRST INITIALIZATION
    # =====================================================

    if not state[
        "initialized"
    ]:
        seen.update(
            x["id"]
            for x in unique
        )

        save_state(
            seen,
            last_search,
            last_official_check,
        )

        print(
            "YouTube baseline saved: "
            f"{len(unique)}"
        )

        return


    # =====================================================
    # NEW VIDEOS
    # =====================================================

    fresh = [
        x
        for x in unique
        if x["id"]
        not in seen
    ]

    for x in reversed(
        fresh
    ):
        notify(
            x["title"],
            x["channel"],
            x["url"],
            x["official"],
        )

        seen.add(
            x["id"]
        )

        print(
            "YouTube sent:",
            x["url"],
        )


    # =====================================================
    # SAVE STATE
    # =====================================================

    seen.update(
        x["id"]
        for x in unique
    )

    save_state(
        seen,
        last_search,
        last_official_check,
    )

    print(
        "Fresh YouTube items: "
        f"{len(fresh)}"
    )

    if not ran_search:
        print(
            "Broad search skipped "
            "on this workflow run."
        )

    if not ran_official:
        print(
            "Official PAE/KAE "
            "feeds skipped on this "
            "workflow run."
        )


if __name__ == "__main__":
    main()
