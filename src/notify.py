"""
Telegram notifications (informational only).

Hard rule: this module SENDS. It never reads Telegram, never polls for
updates, and exposes no command handler. There is therefore no path by which
a Telegram message can instruct this system to do anything -- which is the
requirement that Telegram must not accept live-trading commands.

Credentials come from environment variables (GitHub Actions secrets), never
from files in the repository.
"""
import json
import os
import urllib.parse
import urllib.request

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ENV = "TELEGRAM_CHAT_ID"


def configured() -> bool:
    return bool(os.environ.get(TOKEN_ENV) and os.environ.get(CHAT_ENV))


def send(cfg: dict, text: str) -> bool:
    """Best-effort. A notification failure must never break a scan."""
    if not configured():
        return False
    token = os.environ[TOKEN_ENV]
    chat = os.environ[CHAT_ENV]
    prefix = "[PAPER] " if not cfg.get("live_trading_enabled") else "[LIVE] "
    payload = urllib.parse.urlencode({
        "chat_id": chat,
        "text": (prefix + text)[:4000],
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception:  # noqa: BLE001
        return False
