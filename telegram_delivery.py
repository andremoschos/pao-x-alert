import html
import os
import re
from datetime import datetime, timezone

import requests


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ONLY_PAO_CHAT_ID = os.environ.get("TELEGRAM_ONLY_PAO_CHAT_ID", "").strip()

THREADS = {
    "x_general": os.environ.get("TELEGRAM_THREAD_X_GENERAL", "").strip(),
    "only_panathinaikos_x": os.environ.get("TELEGRAM_THREAD_ONLY_PAO_X", "").strip(),
    "google_news_web": os.environ.get("TELEGRAM_THREAD_GOOGLE", "").strip(),
    "official_pao": os.environ.get("TELEGRAM_THREAD_OFFICIAL", "").strip(),
    "youtube_pao": os.environ.get("TELEGRAM_THREAD_YOUTUBE", "").strip(),
    "system": os.environ.get("TELEGRAM_THREAD_SYSTEM", "").strip(),
    "conference_opponents": os.environ.get("TELEGRAM_THREAD_CONFERENCE", "273").strip(),
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


def _chat_for_route(route):
    if route == "only_panathinaikos_x" and ONLY_PAO_CHAT_ID:
        return ONLY_PAO_CHAT_ID
    return CHAT_ID


def _route_for_ntfy_url(url):
    topic = str(url).rstrip("/").rsplit("/", 1)[-1]
    for route, env_name in NTFY_ROUTE_ENV.items():
        configured_topic = os.environ.get(env_name, "").strip()
        if configured_topic and topic == configured_topic:
            return route
    return None


def _route_header(route, original_title=""):
    title_upper = str(original_title or "").upper()

    if route == "x_general":
        return "🚨 <b>X / TWITTER PAO</b>"
    if route == "only_panathinaikos_x":
        return "☘️ <b>ONLY PANATHINAIKOS X</b>"
    if route == "google_news_web":
        if "TRANSFERFEED" in title_upper:
            return "🔄 <b>DIRECT | TRANSFERFEED</b>"
        return "📰 <b>GOOGLE NEWS + WEB</b>"
    if route == "youtube_pao":
        return "📺 <b>YOUTUBE PAO</b>"
    if route == "system":
        return "🛠️ <b>SYSTEM / RECOVERY</b>"
    if route == "conference_opponents":
        return "🏆 <b>CONFERENCE LEAGUE | ΑΝΤΙΠΑΛΟΙ</b>"

    if route == "official_pao":
        if "KAE" in title_upper:
            return "🏀 <b>ΚΑΕ ΠΑΝΑΘΗΝΑΪΚΟΣ | ΕΠΙΣΗΜΟ</b>"
        if "PAE" in title_upper:
            return "⚽ <b>ΠΑΕ ΠΑΝΑΘΗΝΑΪΚΟΣ | ΕΠΙΣΗΜΟ</b>"
        if "AO" in title_upper:
            return "☘️ <b>ΑΟ ΠΑΝΑΘΗΝΑΪΚΟΣ | ΕΠΙΣΗΜΟ</b>"
        return "🏛️ <b>ΕΠΙΣΗΜΑ ΠΑΟ</b>"

    return "☘️ <b>PAO WATCHER</b>"


def _clean_body(body):
    text = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("\n---\n", "\n━━━━━━━━━━━━\n")
    return text


def _safe_text(route, title, body, click=None):
    header = _route_header(route, title)
    cleaned = _clean_body(body)

    # Keep enough room for the header and optional final link.
    if len(cleaned) > 3550:
        cleaned = cleaned[:3520].rstrip() + "\n…"

    body_html = html.escape(cleaned)
    parts = [header]

    # Avoid repeating the old ntfy title when the Telegram category header
    # already provides the context.
    if body_html:
        parts.append(body_html)

    click = str(click or "").strip()
    if click and click not in cleaned:
        safe_click = html.escape(click, quote=True)
        parts.append(f'🔗 <a href="{safe_click}">Άνοιγμα πηγής</a>')

    text = "\n\n".join(parts)

    # Telegram sendMessage limit is 4096 characters.
    if len(text) > 4000:
        text = text[:3970].rstrip() + "\n…"
    return text


def send(route, title, body, click=None):
    global _last_ok, _last_error, _successful_sends, _failed_sends

    if not configured():
        _last_error = "Telegram secrets missing"
        return False

    thread = THREADS.get(route, "")
    if route == "only_panathinaikos_x" and ONLY_PAO_CHAT_ID:
        thread_id = None
    else:
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

    target_chat_id = _chat_for_route(route)
    payload = {
        "chat_id": target_chat_id,
        "text": _safe_text(route, title, body, click),
        "parse_mode": "HTML",
        "disable_notification": False,
        "link_preview_options": {"is_disabled": True},
    }
    if route != "only_panathinaikos_x" or not ONLY_PAO_CHAT_ID:
        payload["message_thread_id"] = thread_id

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


def _route_target(route):
    thread = THREADS.get(route, "")
    if route == "only_panathinaikos_x" and ONLY_PAO_CHAT_ID:
        return _chat_for_route(route), None
    if not thread:
        return _chat_for_route(route), None
    try:
        return _chat_for_route(route), int(thread)
    except Exception:
        return _chat_for_route(route), None


def _x_caption(route, tweet):
    header = _route_header(route, "X")
    author = html.escape(str(tweet.get("author") or "").strip())
    body = html.escape(str(tweet.get("text") or "").strip())
    link = html.escape(str(tweet.get("url") or "").strip(), quote=True)

    parts = [header]
    if author:
        parts.append(f"<b>{author}</b>")
    if body:
        if len(body) > 760:
            body = body[:757].rstrip() + "…"
        parts.append(body)
    if link:
        parts.append(f'🔗 <a href="{link}">Άνοιγμα ανάρτησης στο X</a>')

    caption = "\n\n".join(parts)
    # Telegram media captions are limited to 1024 chars.
    return caption[:1000]


def _telegram_api(method, payload):
    return requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload,
        timeout=30,
    )


def send_x_post(route, tweet):
    """Send X alerts in the original lightweight text-only format.

    Media is intentionally not embedded and link previews stay disabled. This
    keeps X alerts compact and avoids rendering the whole post in Telegram.
    """
    author = str(tweet.get("author") or "").strip()
    text = " ".join(str(tweet.get("text") or "").split()).strip()
    if len(text) > 280:
        text = text[:277].rstrip() + "…"

    body_parts = []
    if author:
        body_parts.append(author)
    if text:
        body_parts.append(text)

    return send(
        route,
        "X POST",
        "\n".join(body_parts),
        tweet.get("url"),
    )


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
