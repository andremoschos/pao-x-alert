import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
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
STATE_FILES = [
    "seen.json",
    "panathinaikos_seen.json",
    "google_seen.json",
    "official_seen.json",
    "youtube_seen.json",
    "fast_health.json",
]

# ntfy.sh applies visitor/IP rate limits. All watcher modules share this same
# requests module object, so a single wrapper can pace ALL ntfy traffic from the
# Fast runner instead of allowing X/Official/YouTube bursts to collide.
_ORIGINAL_REQUESTS_POST = requests.post
_NTFY_MIN_GAP_SECONDS = 3.0
_NTFY_MAX_ATTEMPTS = 3
_NTFY_MAX_COOLDOWN_SECONDS = 25.0
_last_ntfy_request_at = 0.0


def _rate_limit_delay(response, attempt):
    """Return a safe cooldown using every reset hint ntfy/proxies may expose."""
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
            # Most reset headers are Unix timestamps; small values are seconds.
            if value > now - 60:
                return max(1.0, value - now)
            return max(1.0, value)
        except Exception:
            pass

    return min(20.0, 6.0 * (2 ** (attempt - 1)))


def resilient_post(url, *args, **kwargs):
    global _last_ntfy_request_at

    if not str(url).startswith("https://ntfy.sh/"):
        return _ORIGINAL_REQUESTS_POST(url, *args, **kwargs)

    response = None
    for attempt in range(1, _NTFY_MAX_ATTEMPTS + 1):
        # Keep a visitor-wide floor between sends. This is intentionally shared
        # across every topic/component in this Python process.
        since_last = time.monotonic() - _last_ntfy_request_at
        if since_last < _NTFY_MIN_GAP_SECONDS:
            time.sleep(_NTFY_MIN_GAP_SECONDS - since_last)

        response = _ORIGINAL_REQUESTS_POST(url, *args, **kwargs)
        _last_ntfy_request_at = time.monotonic()

        if response.status_code != 429:
            return response

        # Never let an ntfy cooldown freeze the whole PAO runner for many
        # minutes. If the last retry is still rate-limited, return the 429 so
        # the component records an error and the undelivered item retries next
        # cycle instead of blocking every other watcher.
        if attempt >= _NTFY_MAX_ATTEMPTS:
            print(
                f"ntfy 429 for {url}; retry budget exhausted, "
                "leaving item pending for the next cycle",
                flush=True,
            )
            return response

        delay = _rate_limit_delay(response, attempt)
        delay = max(3.0, min(delay, _NTFY_MAX_COOLDOWN_SECONDS))
        print(
            f"ntfy 429 for {url}; bounded visitor-wide cooldown {delay:.1f}s "
            f"(attempt {attempt}/{_NTFY_MAX_ATTEMPTS})",
            flush=True,
        )
        time.sleep(delay)

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
    # Full official monitor first; direct-X recovery second. Both share
    # official_seen.json, so the second pass is deduplicated automatically.
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

        # IMPORTANT: every component is isolated. A temporary X/API/ntfy
        # failure must never stop Google, Official or YouTube.
        await run_component("x_general", x_general.main, health)
        await run_component("only_panathinaikos_x", only_x.main, health)
        await run_component("google_news_web", google.main, health)
        await run_component("official_pao", official_cycle, health)
        await run_component("youtube_pao", youtube.main, health)

        health["last_cycle_finished_at"] = now_iso()
        health["last_cycle_duration_seconds"] = round(time.time() - started, 2)
        write_health(health)

        try:
            save_states()
        except Exception as exc:
            # State push failure should be visible in health/logs but should not
            # kill the long-running watcher. Retry naturally next cycle.
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
