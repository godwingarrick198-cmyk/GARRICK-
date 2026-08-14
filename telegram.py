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
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )

        if r.is_error:
            try:
                description = r.json().get(
                    "description",
                    "Unknown Telegram error",
                )
            except Exception:
                description = "Telegram returned an invalid error response"

            raise RuntimeError(
                f"Telegram sendMessage failed "
                f"({r.status_code}): {description}"
            )

        r.raise_for_status()
        return True

    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Telegram request failed: {type(e).__name__}"
        ) from e


def send_message(chat_id, text):
    """Send a message to a specific Telegram chat without changing the reporting channel."""
    return _send_to_chat(chat_id, text)


def is_channel_admin(user_id):
    """Return True when the Telegram user is an admin/owner of TELEGRAM_CHAT_ID."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    channel = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not channel or not user_id:
        return False

    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{token}/getChatMember",
            params={
                "chat_id": channel,
                "user_id": user_id,
            },
            timeout=15,
        )

        r.raise_for_status()

        data = r.json()

        if not data.get("ok"):
            return False

        status = (data.get("result") or {}).get("status")

        return status in {"creator", "administrator"}

    except httpx.HTTPError:
        return False


def notify(text):
    """Send a report to the configured Telegram channel."""
    chat = os.getenv("TELEGRAM_CHAT_ID")
    return _send_to_chat(chat, text)


def get_updates(offset=None, timeout=20):
    """Read Telegram updates so the automation can process private bot commands."""
    token, _ = _config()

    params = {
        "timeout": timeout,
        "allowed_updates": ["message", "channel_post"],
    }

    if offset is not None:
        params["offset"] = offset

    try:
        response = httpx.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params=params,
            timeout=timeout + 5,
        )

        response.raise_for_status()

        payload = response.json()

        if not payload.get("ok"):
            description = payload.get(
                "description",
                "Telegram getUpdates returned an error",
            )

            error_code = payload.get(
                "error_code",
                "unknown",
            )

            raise RuntimeError(
                f"Telegram getUpdates failed: "
                f"{error_code}: {description}"
            )

        return payload.get("result", [])

    except httpx.HTTPStatusError as e:
        try:
            data = e.response.json()

            error_code = data.get(
                "error_code",
                e.response.status_code,
            )

            description = data.get(
                "description",
                "No Telegram error description",
            )

        except Exception:
            error_code = e.response.status_code
            description = e.response.text[:500]

        raise RuntimeError(
            f"Telegram polling failed: "
            f"{error_code}: {description}"
        ) from e

    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Telegram polling request failed: "
            f"{type(e).__name__}"
        ) from e
