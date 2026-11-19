import argparse
import json
import os
from pathlib import Path
import secrets

import httpx

from assistant.config import Settings


def load_env(path=Path(".env")):
    if path.is_file():
        for line in path.read_text().splitlines():
            if not line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def init():
    path = Path(".env")
    if path.exists():
        print("Using the existing .env file.")
        return
    config = Path(".env.example").read_text()
    for key in ("API_SERVER_KEY", "A3_INTERNAL_KEY"):
        config = config.replace(f"{key}=\n", f"{key}={secrets.token_hex(32)}\n")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(config)
    print("Created .env with private runtime keys. Add provider and integration settings locally.")


def google_login(args, settings):
    from google_auth_oauthlib.flow import InstalledAppFlow

    from assistant.google import SCOPES

    if not settings.test_email:
        raise SystemExit("Set A3_TEST_EMAIL to the dedicated Google account before authorizing.")
    flow = InstalledAppFlow.from_client_secrets_file(args.client, SCOPES)
    credentials = flow.run_local_server(port=0, prompt="consent", login_hint=settings.test_email)
    result = httpx.get("https://www.googleapis.com/gmail/v1/users/me/profile",
                       headers={"Authorization": "Bearer " + credentials.token}, timeout=15)
    if result.status_code != 200 or result.json().get("emailAddress", "").lower() != settings.test_email.lower():
        raise SystemExit("The authorized account differs from the dedicated test mailbox. Token discarded.")
    path = settings.google_token_file
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(credentials.to_json())
    path.chmod(0o600)
    print("Saved the test Google account token.")


def request(path, data, settings, url=None):
    endpoint = url.rstrip("/") + "/ops/" if url else "http://127.0.0.1:8090/internal/"
    if url and not url.startswith("https://"):
        raise SystemExit("Use the instance's HTTPS URL for remote operations.")
    result = httpx.post(endpoint + path, json=data, timeout=180,
                        headers={"Authorization": "Bearer " + settings.internal_key})
    print(json.dumps(result.json(), indent=2))
    if result.status_code >= 400 or result.json().get("status") == "failed":
        raise SystemExit(1)


def telegram_ids(settings):
    if not settings.telegram_token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first.")
    try:
        response = httpx.get(f"https://api.telegram.org/bot{settings.telegram_token}/getUpdates", timeout=15)
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise SystemExit("The Telegram bot API could not be reached.") from None
    if response.status_code != 200 or not payload.get("ok"):
        raise SystemExit("Telegram could not list updates. Verify the token and stop any bot poller first.")
    identities = set()
    for update in payload["result"]:
        message = update.get("message", {})
        chat, sender = message.get("chat", {}), message.get("from", {})
        if chat.get("type") == "private" and "id" in sender:
            identities.add((sender["id"], chat["id"]))
    if not identities:
        raise SystemExit("Send /start to the bot in a Telegram direct chat, then try again.")
    for user, chat in sorted(identities):
        print(f"TELEGRAM_ALLOWED_USERS={user}\nTELEGRAM_HOME_CHANNEL={chat}")


def main():
    parser = argparse.ArgumentParser(prog="a3")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("telegram-ids")
    auth = sub.add_parser("google-login")
    auth.add_argument("--client", default="secrets/google-oauth-client.json")
    run = sub.add_parser("run-now")
    run.add_argument("job", choices=("briefing", "recap", "check"))
    run.add_argument("--url")
    simulate = sub.add_parser("simulate-429")
    simulate.add_argument("enabled", choices=("on", "off"))
    simulate.add_argument("--url")
    sub.add_parser("start")
    args = parser.parse_args()
    load_env()
    settings = Settings.from_env()
    if args.command == "init":
        init()
    elif args.command == "google-login":
        google_login(args, settings)
    elif args.command == "telegram-ids":
        telegram_ids(settings)
    elif args.command == "run-now":
        request("run", {"job": args.job}, settings, args.url)
    elif args.command == "simulate-429":
        request("simulate", {"enabled": args.enabled == "on"}, settings, args.url)
    elif args.command == "start":
        from assistant.runtime import run_services
        run_services(settings)


if __name__ == "__main__":
    main()
