import httpx


class Telegram:
    def __init__(self, settings):
        self.settings = settings

    def send(self, text, keyboard=None):
        if not self.settings.telegram_token or not self.settings.telegram_chat:
            raise RuntimeError("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_HOME_CHANNEL.")
        data = {"chat_id": self.settings.telegram_chat, "text": text[:4000],
                "link_preview_options": {"is_disabled": True}}
        if keyboard:
            data["reply_markup"] = {"keyboard": keyboard, "resize_keyboard": True, "one_time_keyboard": True}
        # Never log the request URL because it contains the bot token.
        try:
            response = httpx.post(f"https://api.telegram.org/bot{self.settings.telegram_token}/sendMessage",
                                  json=data, timeout=20)
        except httpx.HTTPError as exc:
            raise RuntimeError("Telegram delivery outcome is uncertain. Inspect the chat before retrying.") from exc
        if response.status_code != 200 or not response.json().get("ok"):
            raise RuntimeError("Telegram rejected the delivery. Verify the bot token and destination.")
        return response.json()["result"]["message_id"]
