import hashlib
import json
import os

import httpx


def _config():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("Telegram configuration missing")
    return token, chat


def _send_to_chat(chat_id, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        return False

    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError("Telegram sendMessage failed")
        return True

    except httpx.HTTPStatusError as exc:
        try:
            description = exc.response.json().get(
                "description",
                "Telegram rejected the request",
            )
        except Exception:
            description = "Telegram rejected the request"

        raise RuntimeError(
            f"Telegram sendMessage failed ({exc.response.status_code}): {description}"
        ) from exc

    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Telegram request failed: {type(exc).__name__}"
        ) from exc


def send_message(chat_id, text):
    """Send a private confirmation/message without changing the reporting channel."""
    return _send_to_chat(chat_id, text)


def is_channel_admin(user_id):
    """Authorize Garrick campaign control using the configured Telegram user ID."""
    admin_user_id = os.getenv("TELEGRAM_ADMIN_USER_ID")

    if not admin_user_id or not user_id:
        return False

    return str(user_id).strip() == str(admin_user_id).strip()


def notify(text):
    """Send lead/campaign reports to the configured Telegram channel."""
    chat = os.getenv("TELEGRAM_CHAT_ID")
    return _send_to_chat(chat, text)


def webhook_secret():
    """Return a deterministic webhook secret derived from the private bot token."""
    token, _ = _config()
    return hashlib.sha256(token.encode()).hexdigest()


def set_webhook(public_base_url):
    """Tell Telegram to deliver bot updates to the FastAPI webhook."""
    token, _ = _config()
    base = public_base_url.rstrip("/")
    webhook_url = f"{base}/telegram/webhook"

    response = httpx.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        params={
            "url": webhook_url,
            "allowed_updates": json.dumps(["message", "channel_post"]),
            "drop_pending_updates": "false",
            "secret_token": webhook_secret(),
        },
        timeout=15,
    )
    response.raise_for_status()

    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError("Telegram webhook configuration failed")

    return webhook_url
            
