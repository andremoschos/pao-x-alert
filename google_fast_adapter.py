from concurrent.futures import ThreadPoolExecutor, as_completed

import google_monitor as google


MAX_WORKERS = 6
CONNECT_TIMEOUT_SECONDS = 4
READ_TIMEOUT_SECONDS = 12


def main():
    """Run the existing Google monitor logic with concurrent feed prefetching.

    google_monitor.main() intentionally owns all filtering, dedupe, freshness,
    direct-source suppression, ntfy pacing and state semantics. This adapter
    changes only the slow network collection phase: Google Alerts + all Google
    News editions are fetched concurrently, cached, and then handed back to the
    original main() so behavior stays identical while the Fast cycle remains
    near its 2-minute target.
    """
    original_get = google.requests.get
    original_web_fetch = google.fetch_web_alert
    original_news_fetch = google.fetch_google_news_edition

    def bounded_get(url, *args, **kwargs):
        # Keep one sluggish Google endpoint from holding an entire worker for
        # 30 seconds. The requests happen only during this prefetch phase and
        # the original requests.get is restored before notifications/next
        # watcher components run.
        kwargs["timeout"] = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
        return original_get(url, *args, **kwargs)

    web_result = None
    news_results = {}

    google.requests.get = bounded_get
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(original_web_fetch): ("web", None)}
            for edition in google.NEWS_EDITIONS:
                futures[pool.submit(original_news_fetch, *edition)] = (
                    "news",
                    edition,
                )

            for future in as_completed(futures):
                kind, edition = futures[future]
                try:
                    value = future.result()
                    result = (True, value)
                except Exception as exc:
                    result = (False, exc)

                if kind == "web":
                    web_result = result
                else:
                    news_results[edition] = result
    finally:
        google.requests.get = original_get

    if web_result is None:
        web_result = (False, RuntimeError("Google Web prefetch produced no result"))

    def cached_web_fetch():
        ok, value = web_result
        if ok:
            return value
        raise value

    def cached_news_fetch(country, hl, lang):
        key = (country, hl, lang)
        ok, value = news_results.get(
            key,
            (False, RuntimeError(f"Google News prefetch missing for {country}")),
        )
        if ok:
            return value
        raise value

    google.fetch_web_alert = cached_web_fetch
    google.fetch_google_news_edition = cached_news_fetch
    try:
        return google.main()
    finally:
        google.fetch_web_alert = original_web_fetch
        google.fetch_google_news_edition = original_news_fetch
