import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import google_monitor as google
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
