import json
import os
import sys
import urllib.error
import urllib.request


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ROUTES = [
    ("X / Twitter PAO", os.environ.get("TELEGRAM_THREAD_X_GENERAL", "").strip()),
    ("Only Panathinaikos X", os.environ.get("TELEGRAM_THREAD_ONLY_PAO_X", "").strip()),
    ("Google News + Web", os.environ.get("TELEGRAM_THREAD_GOOGLE", "").strip()),
    ("Official PAO", os.environ.get("TELEGRAM_THREAD_OFFICIAL", "").strip()),
    ("YouTube PAO", os.environ.get("TELEGRAM_THREAD_YOUTUBE", "").strip()),
    ("System / Recovery", os.environ.get("TELEGRAM_THREAD_SYSTEM", "").strip()),
]


def send(label, thread):
    payload = json.dumps(
        {
            "chat_id": CHAT_ID,
            "message_thread_id": int(thread),
            "text": f"✅ PAO Watcher Telegram routing test: {label}",
            "disable_notification": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"Telegram rejected route {label}")
    print(f"OK: {label} -> thread {thread}")


def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram bot token/chat id missing")
    for label, thread in ROUTES:
        if not thread:
            raise RuntimeError(f"Telegram thread missing: {label}")
        send(label, thread)
    print("ALL TELEGRAM ROUTES OK")


if __name__ == "__main__":
    main()
