import base64
from email.message import EmailMessage
from email import message_from_bytes, policy
from email.utils import make_msgid
import hashlib
import hmac
import json
import re
import secrets
import time

from assistant.logging import event


class Email:
    def __init__(self, settings, state, google, telegram, checkpoint):
        self.settings, self.state, self.google = settings, state, google
        self.telegram, self.checkpoint = telegram, checkpoint

    @staticmethod
    def raw_digest(raw):
        return hashlib.sha256(base64.urlsafe_b64decode(raw)).hexdigest()

    @staticmethod
    def preview_content(raw):
        message = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
        if message.get_content_type() != "text/plain" or message.get_content_disposition() == "attachment":
            raise PermissionError("The saved draft no longer matches the preview.")
        headers = tuple(str(message.get(name, "")) for name in
                        ("From", "To", "Cc", "Bcc", "Reply-To", "Subject"))
        return headers, message.get_content().replace("\r\n", "\n")

    def prepare(self, session, recipient, subject, body, request_approval=False):
        context = self.state.session(session)
        if context["private"]:
            raise PermissionError("External email tools are unavailable in private conversations.")
        if recipient.lower() not in self.settings.email_allowlist:
            raise PermissionError("Recipient is outside the dedicated test recipient allowlist.")
        if not subject or len(subject) > 200 or not body or len(body) > 20000:
            raise ValueError("Use a subject up to 200 characters and body up to 20,000 characters.")
        if request_approval and len(body) > 3000:
            raise ValueError("Use up to 3,000 characters so the full draft fits in the Telegram approval preview.")
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.settings.test_email, recipient, subject
        message["Message-ID"] = make_msgid(domain="bluebird.example")
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes(policy=policy.SMTP)).decode()
        draft = self.google.create_draft(raw)
        if not request_approval:
            return {"draft_id": draft, "status": "draft_saved"}
        if context["platform"] != "telegram":
            return {"draft_id": draft, "status": "draft_saved", "message": "Ask in Telegram to approve sending."}
        if not re.search(r"\b(send|reply|email)\b", context["user_message"], re.I):
            raise PermissionError("An explicit email request is required before requesting send approval.")
        if context["sender"] != self.settings.telegram_user:
            raise PermissionError("Only the configured Telegram user can request email approval.")
        # Gmail adds transport headers when it saves a draft. Bind approval to
        # the returned draft after verifying that its preview fields are intact.
        saved_raw = self.google.draft_raw(draft)
        if self.preview_content(saved_raw) != self.preview_content(raw):
            raise PermissionError("The saved draft no longer matches the preview.")
        token = secrets.token_hex(12)
        payload = {"recipient": recipient, "subject": subject, "body": body, "raw": raw}
        with self.state.connect() as db:
            db.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?)",
                       (token, json.dumps(payload), self.raw_digest(saved_raw), session, time.time() + 600,
                        "pending", draft, message["Message-ID"]))
        self.checkpoint()
        self.telegram.send(f"Approve this test email?\nTo: {recipient}\nSubject: {subject}\n\n{body}\n\nExpires in 10 minutes.",
                           [[f"/a3approve {token}", f"/a3reject {token}"]])
        event("email_approval_requested", approvalId=token, expiresInSeconds=600)
        return {"draft_id": draft, "status": "awaiting_human_approval"}

    def decide(self, token, approve, sender, platform):
        if platform != "telegram" or sender != self.settings.telegram_user:
            raise PermissionError("Email approval requires the configured Telegram user.")
        with self.state.connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (token,)).fetchone()
            if row is None or row["status"] != "pending":
                raise ValueError("Approval is unavailable or already used.")
            if row["expires"] < time.time():
                db.execute("UPDATE approvals SET status='expired' WHERE id=?", (token,))
                expired = True
            else:
                expired = False
                db.execute("UPDATE approvals SET status=? WHERE id=?",
                           ("checking" if approve else "rejected", token))
        self.checkpoint()
        if expired:
            raise ValueError("Approval expired. Request a new preview.")
        if not approve:
            event("email_approval_rejected", approvalId=token)
            return {"status": "rejected"}
        payload = json.loads(row["payload"])
        try:
            if payload["recipient"].lower() not in self.settings.email_allowlist:
                raise PermissionError("Recipient is no longer allowed.")
            if not hmac.compare_digest(self.raw_digest(self.google.draft_raw(row["gmail_draft"])), row["digest"]):
                raise PermissionError("The draft changed. Request a fresh preview and approval.")
        except Exception:
            with self.state.connect() as db:
                db.execute("UPDATE approvals SET status='invalidated' WHERE id=?", (token,))
            self.checkpoint()
            raise
        # Commit intent durably before the irreversible API operation. A crash leaves an uncertain send.
        with self.state.connect() as db:
            db.execute("UPDATE approvals SET status='sending' WHERE id=?", (token,))
        self.checkpoint()
        try:
            # Send the approved bytes. A concurrent Gmail edit cannot alter the approved envelope.
            self.google.send_message(payload["raw"])
        except Exception:
            with self.state.connect() as db:
                db.execute("UPDATE approvals SET status='uncertain' WHERE id=?", (token,))
            self.checkpoint()
            event("email_send_uncertain", approvalId=token)
            return {"status": "uncertain", "message": "Inspect Sent mail before taking another action."}
        with self.state.connect() as db:
            db.execute("UPDATE approvals SET status='sent' WHERE id=?", (token,))
        self.checkpoint()
        event("email_sent", approvalId=token)
        return {"status": "sent"}
