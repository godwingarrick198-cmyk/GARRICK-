import os
import httpx


def notify(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat:
        return False

    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat,
                "text": text
            },
            timeout=15
        )

        if r.is_error:
            try:
                data = r.json()
                description = data.get("description", "Unknown Telegram error")
            except Exception:
                description = "Telegram returned an invalid error response"

            raise RuntimeError(
                f"Telegram sendMessage failed ({r.status_code}): {description}"
            )

        r.raise_for_status()
        return True

    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Telegram request failed: {type(e).__name__}"
        ) from e
