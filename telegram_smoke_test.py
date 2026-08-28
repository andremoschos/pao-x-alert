import telegram_delivery as telegram


TESTS = [
    ("x_general", "NEO PANATHINAIKOS POST", "@example\nΔοκιμαστικό X/Twitter alert\nhttps://x.com/example/status/1", "https://x.com/example/status/1"),
    ("only_panathinaikos_x", "X PANATHINAIKOS", "@example\nΔοκιμαστικό Only Panathinaikos X alert\nhttps://x.com/example/status/2", "https://x.com/example/status/2"),
    ("google_news_web", "NEO GOOGLE NEWS: PANATHINAIKOS", "Δοκιμαστική είδηση Παναθηναϊκού\nΠηγή: Example News\nhttps://example.com/pao", "https://example.com/pao"),
    ("official_pao", "OFFICIAL PAO - PAE - WEBSITE", "Δοκιμαστική επίσημη ανακοίνωση ΠΑΕ\nhttps://www.pao.gr/example", "https://www.pao.gr/example"),
    ("youtube_pao", "YOUTUBE: PANATHINAIKOS", "Δοκιμαστικό νέο βίντεο\nExample Channel\nhttps://www.youtube.com/watch?v=example", "https://www.youtube.com/watch?v=example"),
    ("system", "SYSTEM", "Δοκιμαστικό μήνυμα health / recovery", None),
    ("conference_opponents", "CONFERENCE", "Δοκιμαστικό alert αντιπάλων Conference League", "https://www.uefa.com/"),
]


def main():
    if not telegram.configured():
        raise RuntimeError("Telegram bot token/chat id missing")

    for route, title, body, click in TESTS:
        ok = telegram.send(route, title, body, click)
        if not ok:
            raise RuntimeError(
                f"Telegram route failed: {route}; "
                f"{telegram.health_snapshot().get('last_error')}"
            )
        print(f"OK: {route}")

    print("ALL TELEGRAM FORMATTED ROUTES OK")


if __name__ == "__main__":
    main()
