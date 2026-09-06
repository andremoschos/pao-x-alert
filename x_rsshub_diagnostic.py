import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote

import requests

HOSTS = [
    "https://rss.xxu.do",
    "https://rsshub.stsecurity.moe",
    "https://rsshub.isrss.com",
    "https://rsshub-container.folo.is",
]

COMBINED = (
    '"παναθηναϊκός" OR "παναθηναϊκού" OR "παναθηναϊκό" OR '
    '"παναθηναικος" OR "παναθηναικου" OR "παναθηναικο" OR '
    'from:paobc OR from:fmeetsdata'
)
OFFICIAL_COMBINED = "from:paofc_ OR from:Paobcgr OR from:acpanathinaikos"

PATHS = [
    "/twitter/user/paofc_/exclude_rts_replies",
    "/twitter/user/Paobcgr/exclude_rts_replies",
    "/twitter/user/acpanathinaikos/exclude_rts_replies",
    "/twitter/keyword/" + quote("Panathinaikos", safe=""),
    "/twitter/keyword/" + quote("Παναθηναϊκός", safe=""),
    "/twitter/keyword/" + quote(COMBINED, safe=""),
    "/twitter/keyword/" + quote(OFFICIAL_COMBINED, safe=""),
]


def snowflake_datetime(tweet_id):
    ms = (int(tweet_id) >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def inspect(url):
    try:
        r = requests.get(
            url,
            timeout=12,
            headers={
                "User-Agent": "PAO-Watcher-RSSHub-Diagnostic/1.4",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            },
        )
        text = r.text
        ids = [int(x) for x in re.findall(r"/status/(\d+)", text)]
        status_refs = len(ids)
        x_refs = text.count("x.com/") + text.count("twitter.com/")
        feedish = any(marker in text[:1000].lower() for marker in ("<rss", "<feed", "<?xml"))
        latest_id = max(ids) if ids else None
        result = {
            "url": url,
            "status": r.status_code,
            "len": len(text),
            "feedish": feedish,
            "status_refs": status_refs,
            "x_refs": x_refs,
            "latest_id": str(latest_id) if latest_id else None,
            "latest_at": snowflake_datetime(latest_id) if latest_id else None,
            "content_type": r.headers.get("content-type", ""),
            "preview": " ".join(text[:180].split()),
        }
        print("RSSHUB_DIAG", result, flush=True)
        return r.status_code == 200 and feedish and status_refs > 0
    except Exception as exc:
        print("RSSHUB_DIAG", {"url": url, "error": f"{type(exc).__name__}: {exc}"}, flush=True)
        return False


def main():
    urls = [host + path for host in HOSTS for path in PATHS]
    successes = 0
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futures = {pool.submit(inspect, url): url for url in urls}
        for future in as_completed(futures):
            if future.result():
                successes += 1
    print(f"RSSHUB_DIAG_SUMMARY successes={successes}/{len(urls)}", flush=True)
    if successes == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
