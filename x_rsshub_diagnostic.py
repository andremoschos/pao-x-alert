import json
import sys
from datetime import datetime, timezone

import requests

TESTS = [
    ("fx_user", "https://api.fxtwitter.com/2/profile/paofc_/statuses", {"count": 5}),
    ("fx_search", "https://api.fxtwitter.com/2/search", {"q": "Panathinaikos", "feed": "latest", "count": 5}),
    ("proxy_search", "https://twitter.2-38.com/api/fx/2/search", {"q": "Panathinaikos", "feed": "latest", "count": 5}),
    ("proxy_search_gr", "https://twitter.2-38.com/api/fx/2/search", {"q": "Παναθηναϊκός", "feed": "latest", "count": 5}),
    ("proxy_official", "https://twitter.2-38.com/api/fx/2/search", {"q": "from:paofc_ OR from:Paobcgr OR from:acpanathinaikos", "feed": "latest", "count": 5}),
]


def inspect(name, url, params):
    try:
        r = requests.get(
            url,
            params=params,
            timeout=15,
            headers={"User-Agent": "PAO-Watcher-X-Feed-Diagnostic/2.0", "Accept": "application/json"},
        )
        try:
            data = r.json()
        except Exception:
            data = {}
        results = data.get("results") or [] if isinstance(data, dict) else []
        ids = [str(x.get("id")) for x in results if isinstance(x, dict) and str(x.get("id") or "").isdigit()]
        print(
            "X_API_DIAG",
            {
                "name": name,
                "url": r.url,
                "status": r.status_code,
                "results": len(results),
                "valid_ids": len(ids),
                "latest_id": ids[0] if ids else None,
                "code": data.get("code") if isinstance(data, dict) else None,
                "message": str(data.get("message") or "")[:160] if isinstance(data, dict) else "",
                "preview": " ".join(r.text[:180].split()),
            },
            flush=True,
        )
        return r.status_code in (200, 404) and bool(ids)
    except Exception as exc:
        print("X_API_DIAG", {"name": name, "error": f"{type(exc).__name__}: {exc}"}, flush=True)
        return False


def main():
    ok = {name: inspect(name, url, params) for name, url, params in TESTS}
    print("X_API_DIAG_SUMMARY", ok, flush=True)
    if not ok.get("fx_user"):
        sys.exit(2)
    if not (ok.get("fx_search") or ok.get("proxy_search") or ok.get("proxy_search_gr")):
        sys.exit(3)


if __name__ == "__main__":
    main()
