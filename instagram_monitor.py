import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests


STATE_FILE = Path("instagram_seen.json")
HEALTH_FILE = Path("instagram_health.json")

ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
IG_USER_ID = os.getenv("INSTAGRAM_IG_USER_ID", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_INSTAGRAM_CHAT_ID", "").strip()
API_VERSION = (os.getenv("INSTAGRAM_API_VERSION", "").strip() or "v25.0")
POLL_SECONDS = max(180, int(os.getenv("INSTAGRAM_POLL_SECONDS", "300")))
MEDIA_LIMIT = max(3, min(12, int(os.getenv("INSTAGRAM_MEDIA_LIMIT", "6"))))

_DEFAULT_TARGETS = [
    "sport24",
    "sdnagr",
    "eurohoopsnet",
    "gazzettagr",
    "novasportsgr",
]
TARGETS = [
    value.strip().lstrip("@").lower()
    for value in (os.getenv("INSTAGRAM_TARGETS", "") or ",".join(_DEFAULT_TARGETS)).split(",")
    if value.strip()
]

_EXTRA_KEYWORDS = [
    value.strip()
    for value in os.getenv("INSTAGRAM_PAO_KEYWORDS", "").split(",")
    if value.strip()
]

_last_poll_monotonic = 0.0


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _norm(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().split())


def _relevant(caption):
    text = _norm(caption)
    if not text:
        return False

    strong = (
        "παναθηναικ",
        "panathinaik",
        "panathinaikos",
        "paobc",
        "paofc",
        "panathinaikosbc",
        "panathinaikosfc",
        "#pao",
    )
    if any(token in text for token in strong):
        return True

    if re.search(r"(^|[^a-z0-9])pao([^a-z0-9]|$)", text):
        return True

    return any(_norm(keyword) in text for keyword in _EXTRA_KEYWORDS)


def configured():
    return bool(ACCESS_TOKEN and IG_USER_ID and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def missing_configuration():
    missing = []
    if not ACCESS_TOKEN:
        missing.append("INSTAGRAM_ACCESS_TOKEN")
    if not IG_USER_ID:
        missing.append("INSTAGRAM_IG_USER_ID")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_INSTAGRAM_CHAT_ID")
    return missing


def _load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("targets", {})
            return data
    except Exception:
        pass
    return {"targets": {}}


def _save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_health(**updates):
    health = {}
    try:
        health = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        if not isinstance(health, dict):
            health = {}
    except Exception:
        health = {}
    health.update(updates)
    health["updated_at"] = _now_iso()
    HEALTH_FILE.write_text(
        json.dumps(health, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _business_discovery(username):
    fields = (
        f"business_discovery.username({username})"
        "{id,username,media_count,media.limit(%d)"
        "{id,caption,media_type,permalink,timestamp}}" % MEDIA_LIMIT
    )
    response = requests.get(
        f"https://graph.facebook.com/{API_VERSION}/{IG_USER_ID}",
        params={"fields": fields, "access_token": ACCESS_TOKEN},
        timeout=25,
    )

    if response.status_code == 429:
        raise RuntimeError("Instagram Graph API HTTP 429")

    if response.status_code >= 400:
        detail = ""
        try:
            detail = (response.json().get("error") or {}).get("message", "")
        except Exception:
            detail = response.text[:240]
        raise RuntimeError(
            f"Instagram Graph API HTTP {response.status_code} for @{username}: {detail[:240]}"
        )

    payload = response.json()
    account = payload.get("business_discovery") or {}
    media = (account.get("media") or {}).get("data") or []
    return account, [item for item in media if isinstance(item, dict)]


def _telegram_retry_after(response, attempt):
    try:
        value = (response.json().get("parameters") or {}).get("retry_after")
        if value is not None:
            return max(1.0, min(float(value) + 0.5, 65.0))
    except Exception:
        pass
    return min(2 ** attempt, 10)


def _send_telegram(username, item):
    caption = " ".join(str(item.get("caption") or "").split()).strip()
    if len(caption) > 1800:
        caption = caption[:1797].rstrip() + "…"
    permalink = str(item.get("permalink") or "").strip()
    media_type = str(item.get("media_type") or "POST").strip().upper()

    parts = [
        "📸 <b>INSTAGRAM PAO</b>",
        f"<b>@{username}</b> · {media_type}",
    ]
    if caption:
        import html
        parts.append(html.escape(caption))
    if permalink:
        import html
        parts.append(f'🔗 <a href="{html.escape(permalink, quote=True)}">Άνοιγμα στο Instagram</a>')

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "\n\n".join(parts)[:4000],
        "parse_mode": "HTML",
        "disable_notification": False,
        "link_preview_options": {"is_disabled": False},
    }

    response = None
    for attempt in range(3):
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=25,
        )
        if response.status_code != 429:
            break
        if attempt < 2:
            time.sleep(_telegram_retry_after(response, attempt))

    if response is None or response.status_code != 200:
        status = response.status_code if response is not None else "unknown"
        raise RuntimeError(f"Telegram Instagram route HTTP {status}")

    try:
        if not response.json().get("ok"):
            raise RuntimeError("Telegram Instagram route rejected")
    except ValueError as exc:
        raise RuntimeError("Telegram Instagram route invalid response") from exc


def _trim_seen(values):
    values = list(dict.fromkeys(str(value) for value in values if value))
    return values[-1200:]


def main():
    global _last_poll_monotonic

    if not configured():
        _write_health(
            status="disabled",
            missing=missing_configuration(),
            targets=TARGETS,
            poll_seconds=POLL_SECONDS,
        )
        return

    now_mono = time.monotonic()
    if _last_poll_monotonic and now_mono - _last_poll_monotonic < POLL_SECONDS:
        return
    _last_poll_monotonic = now_mono

    state = _load_state()
    delivered = 0
    relevant_new = 0
    scanned = 0
    successful_targets = 0
    errors = {}

    for username in TARGETS:
        target_state = state.setdefault("targets", {}).setdefault(
            username,
            {"initialized": False, "seen": []},
        )
        seen = set(str(value) for value in target_state.get("seen", []))

        try:
            _account, media_items = _business_discovery(username)
            successful_targets += 1
        except Exception as exc:
            errors[username] = f"{type(exc).__name__}: {exc}"
            continue

        scanned += len(media_items)

        # First successful read is a true baseline: never replay old Instagram posts.
        if not target_state.get("initialized"):
            seen.update(str(item.get("id") or "") for item in media_items if item.get("id"))
            target_state["seen"] = _trim_seen(seen)
            target_state["initialized"] = True
            target_state["baselined_at"] = _now_iso()
            continue

        fresh = [
            item for item in media_items
            if str(item.get("id") or "") and str(item.get("id")) not in seen
        ]
        fresh.sort(key=lambda item: str(item.get("timestamp") or ""))

        for item in fresh:
            media_id = str(item.get("id"))
            caption = str(item.get("caption") or "")

            if not _relevant(caption):
                seen.add(media_id)
                continue

            relevant_new += 1
            _send_telegram(username, item)
            seen.add(media_id)
            delivered += 1

        target_state["seen"] = _trim_seen(seen)
        target_state["last_ok"] = _now_iso()

    _save_state(state)

    if successful_targets == 0:
        _write_health(
            status="error",
            last_error="All Instagram Business Discovery targets failed",
            target_errors=errors,
            targets=TARGETS,
            scanned=scanned,
            delivered=delivered,
        )
        raise RuntimeError("All Instagram Business Discovery targets failed")

    _write_health(
        status="ok" if not errors else "degraded",
        last_ok=_now_iso(),
        last_error=None if not errors else "One or more Instagram targets failed",
        target_errors=errors,
        targets=TARGETS,
        successful_targets=successful_targets,
        scanned=scanned,
        relevant_new=relevant_new,
        delivered=delivered,
        poll_seconds=POLL_SECONDS,
        api_version=API_VERSION,
    )

    print(
        f"instagram_pao: targets_ok={successful_targets}/{len(TARGETS)} "
        f"scanned={scanned} relevant_new={relevant_new} delivered={delivered}",
        flush=True,
    )
