import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

import google_fast_adapter as google
import monitor as x_general
import official_monitor as official
import official_x_direct_monitor as official_x_direct
import panathinaikos_monitor as only_x
import youtube_monitor as youtube

POLL_SECONDS = 120
HEALTH = Path("fast_health.json")
NTFY_BUDGET = Path("ntfy_budget.json")
NTFY_LOCAL_DAILY_BUDGET = 240
STATE_FILES = [
    "seen.json",
    "panathinaikos_seen.json",
    "google_seen.json",
    "official_seen.json",
    "youtube_seen.json",
    "fast_health.json",
    "ntfy_budget.json",
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

    _refresh_ntfy_budget_day()
    if _ntfy_budget["count"] >= NTFY_LOCAL_DAILY_BUDGET:
        print(
            f"ntfy local daily budget reached: {_ntfy_budget['count']}/"
            f"{NTFY_LOCAL_DAILY_BUDGET}; leaving item pending",
            flush=True,
        )
        return _synthetic_429(url, "local ntfy daily safety budget reached")

    now_mono = time.monotonic()
    if now_mono < _ntfy_blocked_until:
        remaining = _ntfy_blocked_until - now_mono
        print(
            f"ntfy circuit open; skipping network send to {url} "
            f"for another {remaining:.1f}s",
            flush=True,
        )
        return _synthetic_429(url)

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

    if daily_quota:
        print(
            f"ntfy daily quota 429 for {url}; circuit open until the next "
            f"UTC quota reset ({cooldown:.0f}s). Pending items are preserved.",
            flush=True,
        )
    else:
        print(
            f"ntfy 429 for {url}; circuit opened for {cooldown:.1f}s. "
            "No more ntfy network requests will be made during this cooldown; "
            "undelivered items remain pending for a later cycle.",
            flush=True,
        )
    return response


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

        await run_component("x_general", x_general.main, health)
        await run_component("only_panathinaikos_x", only_x.main, health)
        await run_component("google_news_web", google.main, health)
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
