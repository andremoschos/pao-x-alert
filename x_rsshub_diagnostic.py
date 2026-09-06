import sys
from urllib.parse import quote

import requests

HOSTS = [
    "https://rss.xxu.do",
    "https://rsshub.stsecurity.moe",
    "https://rsshub.isrss.com",
    "https://rsshub-container.folo.is",
]

PATHS = [
    "/twitter/user/paofc_/exclude_rts_replies",
    "/twitter/user/Paobcgr/exclude_rts_replies",
    "/twitter/user/acpanathinaikos/exclude_rts_replies",
    "/twitter/keyword/" + quote("Panathinaikos", safe=""),
    "/twitter/keyword/" + quote("Παναθηναϊκός", safe=""),
]


def inspect(url):
    try:
        r = requests.get(
            url,
            timeout=25,
            headers={
                "User-Agent": "PAO-Watcher-RSSHub-Diagnostic/1.0",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            },
        )
        text = r.text
        status_refs = text.count("/status/")
        x_refs = text.count("x.com/") + text.count("twitter.com/")
        feedish = any(marker in text[:1000].lower() for marker in ("<rss", "<feed", "<?xml"))
        print(
            "RSSHUB_DIAG",
            {
                "url": url,
                "status": r.status_code,
                "len": len(text),
                "feedish": feedish,
                "status_refs": status_refs,
                "x_refs": x_refs,
                "content_type": r.headers.get("content-type", ""),
                "preview": " ".join(text[:180].split()),
            },
            flush=True,
        )
        return r.status_code == 200 and feedish and status_refs > 0
    except Exception as exc:
        print("RSSHUB_DIAG", {"url": url, "error": f"{type(exc).__name__}: {exc}"}, flush=True)
        return False


def main():
    successes = 0
    total = 0
    for host in HOSTS:
        for path in PATHS:
            total += 1
            if inspect(host + path):
                successes += 1
    print(f"RSSHUB_DIAG_SUMMARY successes={successes}/{total}", flush=True)
    if successes == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
