import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import youtube_monitor as ym

STATE = Path("youtube_seen.json")
STALE_AFTER = timedelta(minutes=18)


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def main():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        data = {}

    stamps = [
        parse_dt(data.get("last_search")),
        parse_dt(data.get("last_official_check")),
    ]
    stamps = [x for x in stamps if x is not None]

    now = datetime.now(timezone.utc)
    newest = max(stamps) if stamps else None

    if newest is not None:
        age = now - newest
        print(f"YouTube heartbeat age: {age.total_seconds() / 60:.1f} minutes")
        if age <= STALE_AFTER:
            print("Primary fast watcher is fresh; backup does nothing.")
            return
    else:
        print("No usable YouTube heartbeat found; backup will run.")

    print("YouTube primary watcher looks stale; running independent recovery check.")
    ym.main()


if __name__ == "__main__":
    main()
