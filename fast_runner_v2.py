import asyncio
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

import google_fast_adapter as google
import conference_opponents_monitor as conference
import monitor as x_general
import official_monitor as official
import official_x_direct_monitor as official_x_direct
import panathinaikos_monitor as only_x
import youtube_monitor as youtube
import telegram_delivery as telegram
import transferfeed_monitor as transferfeed

POLL_SECONDS = 120
HEALTH = Path("fast_health.json")
NTFY_BUDGET = Path("ntfy_budget.json")
NTFY_OUTBOX = Path("ntfy_outbox.json")
NTFY_LOCAL_DAILY_BUDGET = 249
NTFY_OUTBOX_MAX_BODY_BYTES = 3500
STATE_FILES = [
    "seen.json",
    "panathinaikos_seen.json",
    "google_seen.json",
    "transferfeed_seen.json",
    "conference_seen.json",
    "official_seen.json",
    "youtube_seen.json",
    "fast_health.json",
    "ntfy_budget.json",
    "ntfy_outbox.json",
]

# ntfy.sh rate-limits by visitor/IP and has a hosted daily message quota. All
# watcher modules share this requests module object, so one wrapper can pace and
# protect every ntfy publication from this Fast process.
_ORIGINAL_REQUESTS_POST = requests.post
_NTFY_MIN_GAP_SECONDS = 5.0
_NTFY_CIRCUIT_MIN_SECONDS = 60.0
_NTFY_CIRCUIT_MAX_SECONDS = 180.0
_last_ntfy_request_at = 0.0
_ntfy_blocked_until = 0.0
_ntfy_blocked_headers = {}
_ntfy_budget = None
_ntfy_last_429 = None


def _utc_day():
    return datetime.now(timezone.utc).date().isoformat()


def _load_ntfy_budget():
    day = _utc_day()
    try:
        data = json.loads(NTFY_BUDGET.read_text(encoding="utf-8"))
        if data.get("day") == day:
            return {"day": day, "count": max(0, int(data.get("count", 0)))}
    except Exception:
        pass
    return {"day": day, "count": 0}


def _save_ntfy_budget():
    if _ntfy_budget is None:
        return
    NTFY_BUDGET.write_text(
        json.dumps(_ntfy_budget, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _refresh_ntfy_budget_day():
    global _ntfy_budget
    if _ntfy_budget is None:
        _ntfy_budget = _load_ntfy_budget()
    current_day = _utc_day()
    if _ntfy_budget.get("day") != current_day:
        _ntfy_budget = {"day": current_day, "count": 0}
        _save_ntfy_budget()


def _load_ntfy_outbox():
    try:
        data = json.loads(NTFY_OUTBOX.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except Exception:
        pass
    return []


def _save_ntfy_outbox(items):
    NTFY_OUTBOX.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _request_body_text(kwargs):
    body = kwargs.get("data", "")
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body or "")


def _queue_ntfy(url, kwargs, reason):
    items = _load_ntfy_outbox()
    body = _request_body_text(kwargs)
    headers = {
        str(k): str(v)
        for k, v in dict(kwargs.get("headers") or {}).items()
        if v is not None
    }
    signature = hashlib.sha256(
        (str(url) + "\n" + body + "\n" + json.dumps(headers, sort_keys=True)).encode("utf-8")
    ).hexdigest()

    if not any(item.get("signature") == signature for item in items):
        items.append(
            {
                "signature": signature,
                "url": str(url),
                "body": body,
                "headers": headers,
                "queued_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
            }
        )
        _save_ntfy_outbox(items)
        print(
            f"ntfy queued durably ({reason}); outbox={len(items)}",
            flush=True,
        )
    return len(items)


def _accepted_response(url, reason="queued for ntfy delivery"):
    response = requests.Response()
    response.status_code = 202
    response.reason = "Accepted"
    response.url = str(url)
    response._content = reason.encode("utf-8")
    return response


def _chunk_outbox_by_topic(items):
    """Coalesce queued publications by topic without dropping message content."""
    groups = []
    by_url = {}
    for item in items:
        by_url.setdefault(item.get("url", ""), []).append(item)

    for url, topic_items in by_url.items():
        current = []
        current_bytes = 0
        for item in topic_items:
            body = str(item.get("body") or "")
            extra = len(body.encode("utf-8")) + (10 if current else 0)
            if current and current_bytes + extra > NTFY_OUTBOX_MAX_BODY_BYTES:
                groups.append((url, current))
                current = []
                current_bytes = 0
            current.append(item)
            current_bytes += extra
        if current:
            groups.append((url, current))
    return groups


def flush_ntfy_outbox():
    """Deliver pending ntfy alerts when quota is available, compacting catch-up."""
    global _last_ntfy_request_at
    global _ntfy_blocked_until
    global _ntfy_blocked_headers

    _refresh_ntfy_budget_day()
    items = _load_ntfy_outbox()
    if not items:
        return 0

    if _ntfy_budget["count"] >= NTFY_LOCAL_DAILY_BUDGET:
        return 0
    if time.monotonic() < _ntfy_blocked_until:
        return 0

    remaining_items = list(items)
    delivered_signatures = set()
    publications = 0

    for url, group in _chunk_outbox_by_topic(items):
        if _ntfy_budget["count"] >= NTFY_LOCAL_DAILY_BUDGET:
            break

        bodies = [str(item.get("body") or "") for item in group]
        body = "\n\n--- CATCH-UP ---\n\n".join(bodies)
        latest_headers = dict(group[-1].get("headers") or {})
        latest_headers["Title"] = (
            latest_headers.get("Title", "PAO WATCHER")
            if len(group) == 1
            else f"PAO WATCHER CATCH-UP - {len(group)} ALERTS"
        )
        latest_headers["Tags"] = latest_headers.get("Tags", "green_circle")

        since_last = time.monotonic() - _last_ntfy_request_at
        if since_last < _NTFY_MIN_GAP_SECONDS:
            time.sleep(_NTFY_MIN_GAP_SECONDS - since_last)

        response = _ORIGINAL_REQUESTS_POST(
            url,
            data=body.encode("utf-8"),
            headers=latest_headers,
            timeout=20,
        )
        _last_ntfy_request_at = time.monotonic()

        if 200 <= response.status_code < 300:
            _ntfy_budget["count"] += 1
            _save_ntfy_budget()
            delivered_signatures.update(item.get("signature") for item in group)
            publications += 1
            print(
                f"ntfy catch-up delivered: {len(group)} queued alerts in 1 publication; "
                f"budget={_ntfy_budget['count']}/{NTFY_LOCAL_DAILY_BUDGET}",
                flush=True,
            )
            continue

        if response.status_code == 429:
            daily_quota = _is_daily_quota_429(response)
            cooldown = (
                _seconds_until_utc_midnight()
                if daily_quota
                else max(
                    _NTFY_CIRCUIT_MIN_SECONDS,
                    min(_rate_limit_delay(response), _NTFY_CIRCUIT_MAX_SECONDS),
                )
            )
            _ntfy_blocked_until = time.monotonic() + cooldown
            _ntfy_blocked_headers = dict(response.headers)
            _record_429(response, cooldown, daily_quota)
            break

        print(
            f"ntfy catch-up delivery failed HTTP {response.status_code}; "
            "queued alerts retained",
            flush=True,
        )
        break

    if delivered_signatures:
        remaining_items = [
            item for item in remaining_items
            if item.get("signature") not in delivered_signatures
        ]
        _save_ntfy_outbox(remaining_items)

    return publications


def _seconds_until_utc_midnight():
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date()
    reset = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)
    return max(1.0, (reset - now).total_seconds() + 5.0)


def _is_daily_quota_429(response):
    try:
        payload = response.json()
        if int(payload.get("code", 0)) == 42908:
            return True
        error = str(payload.get("error", "")).lower()
        if "daily message quota" in error:
            return True
    except Exception:
        pass
    try:
        return "daily message quota" in (response.text or "").lower()
    except Exception:
        return False


def _rate_limit_delay(response):
    """Return cooldown seconds using reset hints ntfy/proxies may expose."""
    now = time.time()

    retry_after = response.headers.get("Retry-After", "").strip()
    if retry_after:
        try:
            return max(1.0, float(retry_after))
        except Exception:
            try:
                retry_dt = parsedate_to_datetime(retry_after)
                if retry_dt.tzinfo is None:
                    retry_dt = retry_dt.replace(tzinfo=timezone.utc)
                return max(1.0, retry_dt.timestamp() - now)
            except Exception:
                pass

    for header in ("X-RateLimit-Reset", "RateLimit-Reset"):
        raw = response.headers.get(header, "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
            if value > now - 60:
                return max(1.0, value - now)
            return max(1.0, value)
        except Exception:
            pass

    return _NTFY_CIRCUIT_MIN_SECONDS


def _synthetic_429(url, message="ntfy circuit breaker open"):
    response = requests.Response()
    response.status_code = 429
    response.reason = "Too Many Requests"
    response.url = str(url)
    response.headers.update(_ntfy_blocked_headers)
    response._content = message.encode("utf-8")
    return response


def _record_429(response, cooldown, daily_quota):
    global _ntfy_last_429
    body = ""
    try:
        body = (response.text or "")[:800]
    except Exception:
        pass
    _ntfy_last_429 = {
        "at": datetime.now(timezone.utc).isoformat(),
        "daily_quota": bool(daily_quota),
        "cooldown_seconds": round(float(cooldown), 1),
        "body": body,
        "retry_after": response.headers.get("Retry-After"),
        "x_ratelimit_reset": response.headers.get("X-RateLimit-Reset"),
        "ratelimit_reset": response.headers.get("RateLimit-Reset"),
    }


def resilient_post(url, *args, **kwargs):
    global _last_ntfy_request_at
    global _ntfy_blocked_until
    global _ntfy_blocked_headers
    global _ntfy_budget

    if not str(url).startswith("https://ntfy.sh/"):
        return _ORIGINAL_REQUESTS_POST(url, *args, **kwargs)

    telegram_ok = telegram.send_for_ntfy(url, kwargs)
    if telegram_ok:
        print(
            f"Telegram primary delivered for {url.rsplit('/', 1)[-1]}; "
            "ntfy fallback not needed",
            flush=True,
        )
        return _accepted_response(
            url,
            "Telegram primary delivered; ntfy fallback not needed",
        )

    snapshot = telegram.health_snapshot()
    print(
        f"Telegram primary unavailable for {url.rsplit('/', 1)[-1]}: "
        f"{snapshot.get('last_error')}; using ntfy fallback",
        flush=True,
    )

    _refresh_ntfy_budget_day()
    if _ntfy_budget["count"] >= NTFY_LOCAL_DAILY_BUDGET:
        if telegram_ok:
            return _accepted_response(
                url,
                "Telegram primary delivered; ntfy backup skipped at daily safety budget",
            )
        _queue_ntfy(url, kwargs, "local daily safety budget reached")
        return _accepted_response(url, "queued: local ntfy daily safety budget reached")

    now_mono = time.monotonic()
    if now_mono < _ntfy_blocked_until:
        remaining = _ntfy_blocked_until - now_mono
        if telegram_ok:
            print(
                f"ntfy circuit open; Telegram primary already delivered; "
                f"backup skipped for {remaining:.1f}s",
                flush=True,
            )
            return _accepted_response(
                url,
                "Telegram primary delivered while ntfy circuit is open",
            )
        _queue_ntfy(url, kwargs, "ntfy cooldown circuit open")
        print(
            f"ntfy circuit open; queued delivery for {url}; "
            f"cooldown remaining {remaining:.1f}s",
            flush=True,
        )
        return _accepted_response(url, "queued while ntfy circuit is open")

    since_last = now_mono - _last_ntfy_request_at
    if since_last < _NTFY_MIN_GAP_SECONDS:
        time.sleep(_NTFY_MIN_GAP_SECONDS - since_last)

    response = _ORIGINAL_REQUESTS_POST(url, *args, **kwargs)
    _last_ntfy_request_at = time.monotonic()

    if 200 <= response.status_code < 300:
        _ntfy_blocked_until = 0.0
        _ntfy_blocked_headers = {}
        _ntfy_budget["count"] += 1
        _save_ntfy_budget()
        return response

    if response.status_code != 429:
        if telegram_ok:
            return _accepted_response(
                url,
                f"Telegram primary delivered; ntfy backup HTTP {response.status_code}",
            )
        return response

    daily_quota = _is_daily_quota_429(response)
    if daily_quota:
        cooldown = _seconds_until_utc_midnight()
    else:
        hinted_delay = _rate_limit_delay(response)
        cooldown = max(
            _NTFY_CIRCUIT_MIN_SECONDS,
            min(hinted_delay, _NTFY_CIRCUIT_MAX_SECONDS),
        )

    _ntfy_blocked_until = time.monotonic() + cooldown
    _ntfy_blocked_headers = dict(response.headers)
    _record_429(response, cooldown, daily_quota)

    if telegram_ok:
        print(
            f"ntfy 429 ignored as backup failure because Telegram primary delivered; "
            f"cooldown={cooldown:.1f}s",
            flush=True,
        )
        return _accepted_response(url, "Telegram primary delivered; ntfy backup rate-limited")

    _queue_ntfy(
        url,
        kwargs,
        "hosted daily quota" if daily_quota else "temporary ntfy rate limit",
    )

    print(
        f"ntfy 429 converted to durable queued delivery; cooldown={cooldown:.1f}s",
        flush=True,
    )
    return _accepted_response(url, "queued after ntfy 429")


requests.post = resilient_post


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_health():
    try:
        data = json.loads(HEALTH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"components": {}}


def write_health(health):
    HEALTH.write_text(
        json.dumps(health, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def ntfy_health_snapshot():
    _refresh_ntfy_budget_day()
    return {
        "local_budget_day_utc": _ntfy_budget["day"],
        "local_successful_publications": _ntfy_budget["count"],
        "local_daily_budget": NTFY_LOCAL_DAILY_BUDGET,
        "circuit_remaining_seconds": round(
            max(0.0, _ntfy_blocked_until - time.monotonic()), 1
        ),
        "last_429": _ntfy_last_429,
        "outbox_pending": len(_load_ntfy_outbox()),
        "telegram": telegram.health_snapshot(),
    }


def run(cmd, check=True):
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def save_states():
    existing = [name for name in STATE_FILES if Path(name).exists()]
    if not existing:
        return

    run(["git", "config", "user.name", "pao-x-alert"])
    run(["git", "config", "user.email", "actions@users.noreply.github.com"])
    run(["git", "add", "--", *existing])

    staged = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        return

    run(["git", "commit", "-m", "Update fast watcher states"])

    for attempt in range(1, 6):
        pull = subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            text=True,
            capture_output=True,
        )
        if pull.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], capture_output=True)
            print(f"state pull/rebase attempt {attempt} failed: {pull.stderr.strip()}", flush=True)
            time.sleep(attempt * 2)
            continue

        push = subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            text=True,
            capture_output=True,
        )
        if push.returncode == 0:
            print("Fast watcher states pushed", flush=True)
            return

        print(f"state push attempt {attempt} failed: {push.stderr.strip()}", flush=True)
        time.sleep(attempt * 2)

    raise RuntimeError("Could not push fast watcher states after 5 attempts")


async def run_component(name, func, health):
    started = time.time()
    stamp = now_iso()
    try:
        result = func()
        if asyncio.iscoroutine(result):
            await result
        health.setdefault("components", {}).setdefault(name, {})
        health["components"][name].update(
            {
                "status": "ok",
                "last_ok": now_iso(),
                "last_error": None,
                "last_duration_seconds": round(time.time() - started, 2),
            }
        )
        print(f"{name}: OK", flush=True)
    except Exception as exc:
        health.setdefault("components", {}).setdefault(name, {})
        health["components"][name].update(
            {
                "status": "error",
                "last_attempt": stamp,
                "last_error": f"{type(exc).__name__}: {exc}",
                "last_duration_seconds": round(time.time() - started, 2),
            }
        )
        print(f"{name}: ERROR {type(exc).__name__}: {exc}", flush=True)


async def official_cycle():
    await official.main()
    await official_x_direct.main()


async def main():
    health = load_health()
    cycle = int(health.get("cycle", 0))

    while True:
        cycle += 1
        started = time.time()
        health["cycle"] = cycle
        health["runner_started_or_alive_at"] = now_iso()

        try:
            flushed = flush_ntfy_outbox()
            if flushed:
                print(f"ntfy outbox flush publications={flushed}", flush=True)
        except Exception as exc:
            print(f"NTFY OUTBOX FLUSH ERROR: {type(exc).__name__}: {exc}", flush=True)

        await run_component("x_general", x_general.main, health)
        await run_component("only_panathinaikos_x", only_x.main, health)
        await run_component("google_news_web", google.main, health)
        await run_component("transferfeed_panathinaikos", transferfeed.main, health)
        await run_component("conference_opponents", conference.main, health)
        await run_component("official_pao", official_cycle, health)
        await run_component("youtube_pao", youtube.main, health)

        health["last_cycle_finished_at"] = now_iso()
        health["last_cycle_duration_seconds"] = round(time.time() - started, 2)
        health["ntfy"] = ntfy_health_snapshot()
        write_health(health)

        try:
            save_states()
        except Exception as exc:
            health["state_push_error"] = f"{type(exc).__name__}: {exc}"
            write_health(health)
            print(f"STATE SAVE ERROR: {exc}", flush=True)
        else:
            health.pop("state_push_error", None)

        elapsed = time.time() - started
        sleep_for = max(5, POLL_SECONDS - elapsed)
        print(
            f"cycle={cycle} duration={elapsed:.1f}s next_cycle_in={sleep_for:.1f}s",
            flush=True,
        )
        await asyncio.sleep(sleep_for)


if __name__ == "__main__":
    asyncio.run(main())
