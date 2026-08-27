from concurrent.futures import ThreadPoolExecutor, as_completed

import google_monitor as google


MAX_WORKERS = 6
CONNECT_TIMEOUT_SECONDS = 4
READ_TIMEOUT_SECONDS = 12
FAST_MAX_SEND_PER_RUN = 16
MAX_NTFY_BODY_BYTES = 3500


def _format_entry(entry):
    title = entry.get("title") or "(χωρίς τίτλο)"
    if len(title) > 600:
        title = title[:597] + "..."

    lines = [title]
    if entry.get("publisher"):
        lines.append(f"Πηγή: {entry['publisher']}")
    if entry.get("source") == "NEWS":
        lines.append(f"Google News edition: {entry.get('edition', '')}")
    if entry.get("url"):
        lines.append(entry["url"])
    return "\n".join(lines)


def _entry_batches(entries):
    batches = []
    current = []
    for entry in entries:
        candidate = current + [entry]
        body = "\n\n---\n\n".join(_format_entry(item) for item in candidate)
        if current and len(body.encode("utf-8")) > MAX_NTFY_BODY_BYTES:
            batches.append(current)
            current = [entry]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def _send_batch(entries):
    source = entries[0].get("source") or "NEWS"
    count = len(entries)
    if source == "NEWS":
        title = "NEO GOOGLE NEWS: PANATHINAIKOS"
        tags = "newspaper"
    else:
        title = "NEO GOOGLE WEB: PANATHINAIKOS"
        tags = "mag"

    if count > 1:
        title = f"{title} - {count} NEA"

    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": tags,
    }
    if entries[-1].get("url"):
        headers["Click"] = entries[-1]["url"]

    body = "\n\n---\n\n".join(_format_entry(entry) for entry in entries)
    response = google.requests.post(
        f"https://ntfy.sh/{google.TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()


def main():
    """Run Google discovery fast, then batch ntfy publications safely.

    Google keeps its filtering, dedupe and freshness rules. During google.main()
    we temporarily queue notification candidates and defer the state write. Only
    IDs belonging to batches that ntfy actually accepts are persisted as seen;
    failed/blocked entries stay pending for a later cycle.
    """
    original_get = google.requests.get
    original_web_fetch = google.fetch_web_alert
    original_news_fetch = google.fetch_google_news_edition
    original_notify = google.notify
    original_save_state = google.save_state
    original_max_send = google.MAX_SEND_PER_RUN
    original_send_gap = google.SEND_GAP_SECONDS

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

    queued = []
    captured_seen = None

    def queue_notify(entry):
        queued.append(entry)
        return True

    def defer_save_state(ids):
        nonlocal captured_seen
        captured_seen = set(ids)

    google.fetch_web_alert = cached_web_fetch
    google.fetch_google_news_edition = cached_news_fetch
    google.notify = queue_notify
    google.save_state = defer_save_state
    google.MAX_SEND_PER_RUN = FAST_MAX_SEND_PER_RUN
    google.SEND_GAP_SECONDS = 0

    try:
        google.main()
    finally:
        google.fetch_web_alert = original_web_fetch
        google.fetch_google_news_edition = original_news_fetch
        google.notify = original_notify
        google.save_state = original_save_state
        google.MAX_SEND_PER_RUN = original_max_send
        google.SEND_GAP_SECONDS = original_send_gap

    if captured_seen is None:
        return

    queued_ids = {entry["id"] for entry in queued}
    base_seen = set(captured_seen) - queued_ids
    delivered_ids = set()

    try:
        for source in ("NEWS", "WEB"):
            group = [entry for entry in queued if entry.get("source") == source]
            for batch in _entry_batches(group):
                _send_batch(batch)
                batch_ids = {entry["id"] for entry in batch}
                delivered_ids.update(batch_ids)
                original_save_state(base_seen | delivered_ids)
                print(f"Google ntfy batch sent: source={source} items={len(batch)}")
    except Exception:
        original_save_state(base_seen | delivered_ids)
        raise

    original_save_state(base_seen | delivered_ids)
    pending = len(queued_ids - delivered_ids)
    print(
        f"Google batched delivery complete: queued={len(queued)}, "
        f"delivered={len(delivered_ids)}, pending={pending}"
    )


if __name__ == "__main__":
    main()
