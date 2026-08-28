import os
from datetime import datetime, timezone

import requests


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

THREADS = {
    "x_general": os.environ.get("TELEGRAM_THREAD_X_GENERAL", "").strip(),
    "only_panathinaikos_x": os.environ.get("TELEGRAM_THREAD_ONLY_PAO_X", "").strip(),
    "google_news_web": os.environ.get("TELEGRAM_THREAD_GOOGLE", "").strip(),
    "official_pao": os.environ.get("TELEGRAM_THREAD_OFFICIAL", "").strip(),
    "youtube_pao": os.environ.get("TELEGRAM_THREAD_YOUTUBE", "").strip(),
    "system": os.environ.get("TELEGRAM_THREAD_SYSTEM", "").strip(),
}

NTFY_ROUTE_ENV = {
    "x_general": "NTFY_TOPIC",
    "only_panathinaikos_x": "NTFY_PANATHINAIKOS_TOPIC",
    "google_news_web": "NTFY_GOOGLE_TOPIC",
    "official_pao": "NTFY_OFFICIAL_TOPIC",
    "youtube_pao": "NTFY_YOUTUBE_TOPIC",
}

_last_ok = None
_last_error = None
_successful_sends = 0
_failed_sends = 0


def configured():
    return bool(BOT_TOKEN and CHAT_ID)


def _route_for_ntfy_url(url):
    topic = str(url).rstrip("/").rsplit("/", 1)[-1]
    for route, env_name in NTFY_ROUTE_ENV.items():
        configured_topic = os.environ.get(env_name, "").strip()
        if configured_topic and topic == configured_topic:
            return route
    return None


def _safe_text(title, body, click=None):
    parts = []
    if title:
        parts.append(str(title).strip())
    if body:
        parts.append(str(body).strip())
    text = "\n\n".join(part for part in parts if part)

    if click:
        click = str(click).strip()
        if click and click not in text:
            text = f"{text}\n\n{click}" if text else click

    # Telegram sendMessage allows 4096 characters. Keep a little headroom.
    if len(text) > 4000:
        text = text[:3970] + "\n\n…[truncated]"
    return text or "PAO Watcher alert"


def send(route, title, body, click=None):
    global _last_ok, _last_error, _successful_sends, _failed_sends

    if not configured():
        _last_error = "Telegram secrets missing"
        return False

    thread = THREADS.get(route, "")
    if not thread:
        _last_error = f"Telegram thread missing for route={route}"
        _failed_sends += 1
        return False

    try:
        thread_id = int(thread)
    except Exception:
        _last_error = f"Invalid Telegram thread id for route={route}"
        _failed_sends += 1
        return False

    payload = {
        "chat_id": CHAT_ID,
        "message_thread_id": thread_id,
        "text": _safe_text(title, body, click),
        "disable_notification": False,
        "link_preview_options": {"is_disabled": True},
    }

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=20,
        )
        if response.status_code != 200:
            _last_error = f"Telegram HTTP {response.status_code} route={route}"
            _failed_sends += 1
            return False
        data = response.json()
        if not data.get("ok"):
            _last_error = f"Telegram API rejected route={route}"
            _failed_sends += 1
            return False
    except Exception as exc:
        _last_error = f"{type(exc).__name__}: Telegram send failed route={route}"
        _failed_sends += 1
        return False

    _last_ok = datetime.now(timezone.utc).isoformat()
    _last_error = None
    _successful_sends += 1
    return True


def send_for_ntfy(url, kwargs):
    route = _route_for_ntfy_url(url)
    if not route:
        return False

    headers = dict(kwargs.get("headers") or {})
    body = kwargs.get("data", "")
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    else:
        body = str(body or "")

    return send(
        route=route,
        title=headers.get("Title", "PAO Watcher"),
        body=body,
        click=headers.get("Click"),
    )


def health_snapshot():
    return {
        "configured": configured(),
        "last_ok": _last_ok,
        "last_error": _last_error,
        "successful_sends": _successful_sends,
        "failed_sends": _failed_sends,
    }
