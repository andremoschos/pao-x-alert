import sys

import x_rsshub_fallback as x

ACCOUNTS = [
    "paofc_",
    "Paobcgr",
    "acpanathinaikos",
    "paobc",
    "fmeetsdata",
    "Onlypao_gr",
    "papanikolaouchs",
]

REQUIRED = {"paofc_", "onlypao_gr", "papanikolaouchs"}


def main():
    ok = {}
    for username in ACCOUNTS:
        try:
            tweets = x.fetch_user(username, 5)
            ok[username.lower()] = bool(tweets)
            print(
                "X_TIMELINE_DIAG",
                {
                    "account": username,
                    "ok": bool(tweets),
                    "count": len(tweets),
                    "latest": tweets[0]["url"] if tweets else None,
                },
                flush=True,
            )
        except Exception as exc:
            ok[username.lower()] = False
            print(
                "X_TIMELINE_DIAG",
                {
                    "account": username,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                flush=True,
            )

    try:
        aggregate = x.fetch_users(ACCOUNTS, 100)
    except Exception as exc:
        print(
            "X_TIMELINE_AGGREGATE",
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            flush=True,
        )
        sys.exit(2)

    authors = {str(item.get("author") or "").lstrip("@").lower() for item in aggregate}
    print(
        "X_TIMELINE_AGGREGATE",
        {
            "ok": True,
            "count": len(aggregate),
            "required_accounts_ok": {name: ok.get(name, False) for name in sorted(REQUIRED)},
            "authors_present": sorted(authors),
        },
        flush=True,
    )

    if not all(ok.get(name, False) for name in REQUIRED):
        sys.exit(3)


if __name__ == "__main__":
    main()
