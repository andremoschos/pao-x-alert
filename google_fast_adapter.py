from concurrent.futures import ThreadPoolExecutor, as_completed

import google_monitor as google


MAX_WORKERS = 6
CONNECT_TIMEOUT_SECONDS = 4
READ_TIMEOUT_SECONDS = 12
FAST_MAX_SEND_PER_RUN = 4


def main():
    """Run the existing Google monitor logic without letting it stall Fast.

    The original google_monitor.main() still owns filtering, dedupe, freshness,
    direct-source suppression, ntfy retry/pacing and state semantics. This
    adapter changes only two Fast-runner concerns:

    1. Google Alerts + Google News editions are prefetched concurrently with
       bounded network timeouts.
    2. At most a small number of Google notifications are delivered per Fast
       cycle. Unsent recent entries are NOT marked seen by google_monitor, so
       they remain pending and are delivered on following cycles rather than
       being lost. This prevents ntfy's visitor-wide pacing from turning one
       Google burst into a 90+ second blocker for every other PAO watcher.
    """
    original_get = google.requests.get
    original_web_fetch = google.fetch_web_alert
    original_news_fetch = google.fetch_google_news_edition
    original_max_send = google.MAX_SEND_PER_RUN

    def bounded_get(url, *args, **kwargs):
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
    google.MAX_SEND_PER_RUN = FAST_MAX_SEND_PER_RUN
    try:
        return google.main()
    finally:
        google.fetch_web_alert = original_web_fetch
        google.fetch_google_news_edition = original_news_fetch
        google.MAX_SEND_PER_RUN = original_max_send
