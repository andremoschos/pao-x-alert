import json
import os
import sys
import urllib.request


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    thread = os.environ.get("TELEGRAM_THREAD_SYSTEM", "").strip()
    message = " ".join(sys.argv[1:]).strip() or "PAO Watcher system event"

    if not token or not chat_id or not thread:
        print("Telegram System route not configured")
        return 0

    payload = json.dumps(
        {
            "chat_id": chat_id,
            "message_thread_id": int(thread),
            "text": message,
            "disable_notification": False,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))

    if not data.get("ok"):
        raise RuntimeError("Telegram System notification rejected")
    print("Telegram System notification sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
