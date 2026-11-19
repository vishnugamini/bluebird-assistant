import base64
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from pathlib import Path
import threading

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import httpx


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.compose",
          "https://www.googleapis.com/auth/calendar.events.readonly"]


class Google:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.client = httpx.Client(timeout=30, follow_redirects=False)

    def request(self, method, path, **kwargs):
        token_file = self.settings.google_token_file
        if not token_file.exists():
            raise RuntimeError("Authorize the dedicated Google test account with a3 google-login.")
        with self.lock:
            credentials = Credentials.from_authorized_user_file(token_file, SCOPES)
            if not credentials.valid:
                credentials.refresh(Request())
                temporary = Path(str(token_file) + ".tmp")
                temporary.write_text(credentials.to_json())
                temporary.chmod(0o600)
                temporary.replace(token_file)
            response = self.client.request(method, "https://www.googleapis.com/" + path,
                                           headers={"Authorization": "Bearer " + credentials.token}, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"Google request failed with HTTP {response.status_code}. Check account authorization.")
        return response.json() if response.content else {}

    def verify_account(self):
        actual = self.request("GET", "gmail/v1/users/me/profile")["emailAddress"]
        if actual.lower() != self.settings.test_email.lower():
            raise PermissionError("Authorized mailbox differs from A3_TEST_EMAIL.")

    def emails(self, query="is:unread", limit=10):
        self.verify_account()
        listing = self.request("GET", "gmail/v1/users/me/messages", params={"q": query, "maxResults": min(limit, 20)})
        results = []
        for row in listing.get("messages", []):
            item = self.request("GET", f"gmail/v1/users/me/messages/{row['id']}", params={"format": "raw"})
            message = message_from_bytes(base64.urlsafe_b64decode(item["raw"]), policy=policy.default)
            part = message.get_body(preferencelist=("plain",)) if message.is_multipart() else message
            text = part.get_content() if part and part.get_content_type() == "text/plain" else item.get("snippet", "")
            results.append({"id": row["id"], "from": str(message.get("From", "")),
                            "subject": str(message.get("Subject", "")), "body": str(text)[:8000],
                            "labels": item.get("labelIds", [])})
        return {"untrusted_email_content": results}

    def events(self, start=None, end=None):
        if not self.settings.calendar_id:
            raise RuntimeError("Set A3_CALENDAR_ID to the dedicated test calendar.")
        from urllib.parse import quote
        now = datetime.now(timezone.utc)
        start = start or now.isoformat()
        end = end or (now + timedelta(days=2)).isoformat()
        rows, token = [], None
        while True:
            params = {"timeMin": start, "timeMax": end, "singleEvents": "true", "orderBy": "startTime",
                      "maxResults": 250}
            if token:
                params["pageToken"] = token
            result = self.request("GET", f"calendar/v3/calendars/{quote(self.settings.calendar_id, safe='')}/events",
                                  params=params)
            rows.extend({key: item[key] for key in ("id", "summary", "start", "end", "description", "status")
                        if key in item} for item in result.get("items", []) if item.get("status") != "cancelled")
            token = result.get("nextPageToken")
            if not token:
                return rows

    def create_draft(self, raw):
        self.verify_account()
        return self.request("POST", "gmail/v1/users/me/drafts", json={"message": {"raw": raw}})["id"]

    def draft_raw(self, draft):
        return self.request("GET", f"gmail/v1/users/me/drafts/{draft}", params={"format": "raw"})["message"]["raw"]

    def send_message(self, raw):
        self.verify_account()
        return self.request("POST", "gmail/v1/users/me/messages/send", json={"raw": raw})
