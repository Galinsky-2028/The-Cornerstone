"""Email. Backends:

* ``console`` (default): prints the message and keeps a copy in data/outbox.
* ``smtp``: any SMTP server (Render's free plan blocks the SMTP ports).
* ``http``: an HTTPS email API on port 443, which works on any host. Set ``APP_EMAIL_API_URL``, ``APP_EMAIL_API_KEY`` and
  ``APP_EMAIL_FROM``. Request formats for Resend, Postmark and SendGrid are built in; ``APP_EMAIL_API_STYLE`` picks one,
  otherwise it is taken from the URL's host (unknown hosts use the Resend-style JSON that many providers accept).

If the http backend is selected but not fully configured, messages go to the console backend and an error is logged, so
password resets and invitations never crash a request.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import smtplib
import time
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import settings

log = logging.getLogger(__name__)

STYLES = ("resend", "postmark", "sendgrid")
TIMEOUT_SECONDS = 20
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class EmailError(RuntimeError):
    """The provider refused or could not be reached. The message text never contains the API key."""


def _clean(value: str, limit: int = 998) -> str:
    """Header-like fields may not carry CR/LF (no header injection) and are length-limited."""
    return " ".join(str(value or "").splitlines()).strip()[:limit]


def api_style(url: str, configured: str = "") -> str:
    if configured:
        if configured not in STYLES:
            raise ValueError(f"APP_EMAIL_API_STYLE must be one of {', '.join(STYLES)}")
        return configured
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("postmarkapp.com"):
        return "postmark"
    if host.endswith("sendgrid.com"):
        return "sendgrid"
    return "resend"


def http_configured() -> bool:
    url = settings.email_api_url
    return bool(url and url.startswith("https://") and settings.email_api_key and settings.email_from)


def build_http_request(style: str, *, sender: str, to: str, subject: str, body: str, html: str | None,
                       reply_to: str | None, attachments: list[tuple[str, str, bytes]], api_key: str) -> tuple[dict, dict]:
    """Returns (headers, json) for one message in the provider's format."""
    files = [(name, ctype, base64.b64encode(data).decode()) for name, ctype, data in attachments]
    if style == "postmark":
        payload = {"From": sender, "To": to, "Subject": subject, "TextBody": body, "MessageStream": "outbound"}
        if html:
            payload["HtmlBody"] = html
        if reply_to:
            payload["ReplyTo"] = reply_to
        if files:
            payload["Attachments"] = [{"Name": n, "ContentType": c, "Content": d} for n, c, d in files]
        return {"X-Postmark-Server-Token": api_key, "Accept": "application/json"}, payload
    if style == "sendgrid":
        content = [{"type": "text/plain", "value": body}] + ([{"type": "text/html", "value": html}] if html else [])
        name, address = parseaddr(sender)
        payload = {"personalizations": [{"to": [{"email": to}]}], "from": {"email": address, **({"name": name} if name else {})},
                   "subject": subject, "content": content}
        if reply_to:
            payload["reply_to"] = {"email": reply_to}
        if files:
            payload["attachments"] = [{"filename": n, "type": c, "content": d} for n, c, d in files]
        return {"Authorization": f"Bearer {api_key}"}, payload
    payload = {"from": sender, "to": [to], "subject": subject, "text": body}
    if html:
        payload["html"] = html
    if reply_to:
        payload["reply_to"] = reply_to
    if files:
        payload["attachments"] = [{"filename": n, "content": d, "content_type": c} for n, c, d in files]
    return {"Authorization": f"Bearer {api_key}"}, payload


def _send_http(to: str, subject: str, body: str, html: str | None, attachments: list[Path], reply_to: str | None,
               client: httpx.Client | None = None) -> dict:
    files = []
    for path in attachments:
        data = Path(path).read_bytes()
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise EmailError(f"Attachment {Path(path).name} is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
        files.append((Path(path).name, mimetypes.guess_type(Path(path).name)[0] or "application/octet-stream", data))
    style = api_style(settings.email_api_url, settings.email_api_style)
    headers, payload = build_http_request(style, sender=_clean(settings.email_from), to=_clean(to, 320), subject=_clean(subject),
                                          body=body, html=html, reply_to=_clean(reply_to, 320) if reply_to else None,
                                          attachments=files, api_key=settings.email_api_key)
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)
    try:
        response = client.post(settings.email_api_url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise EmailError(f"Email provider unreachable: {type(e).__name__}") from None
    finally:
        if own:
            client.close()
    if response.status_code >= 300:
        raise EmailError(f"Email provider refused the message: HTTP {response.status_code}")
    receipt = ""
    try:
        data = response.json()
        receipt = str(data.get("id") or data.get("MessageID") or "")[:200] if isinstance(data, dict) else ""
    except ValueError:
        pass
    receipt = receipt or response.headers.get("x-message-id", "")[:200]
    log.info("email accepted by %s for %s: %s", style, to, subject)
    return {"backend": "http", "style": style, "to": to, "receipt": receipt}


def send_email(to: str, subject: str, body: str, html: str | None = None, attachments: list[Path] | None = None,
               reply_to: str | None = None, *, http_client: httpx.Client | None = None) -> dict:
    if not to:
        raise ValueError("No recipient")
    backend = settings.email_backend
    if backend == "http":
        if http_configured():
            return _send_http(to, subject, body, html, list(attachments or []), reply_to, client=http_client)
        log.error("APP_EMAIL_BACKEND=http but APP_EMAIL_API_URL (https), APP_EMAIL_API_KEY or APP_EMAIL_FROM is missing; "
                  "writing the message to the console outbox instead")
        backend = "console"
    msg = EmailMessage()
    msg["From"] = settings.email_from or settings.smtp_user or f"no-reply@{settings.app_name.lower().replace(' ', '')}.local"
    msg["To"] = _clean(to, 320)
    msg["Subject"] = _clean(subject)
    if reply_to:
        msg["Reply-To"] = _clean(reply_to, 320)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for path in attachments or []:
        ctype = mimetypes.guess_type(Path(path).name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(Path(path).read_bytes(), maintype=maintype, subtype=subtype, filename=Path(path).name)
    if backend == "smtp":
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as s:
            if settings.smtp_starttls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        log.info("email sent to %s: %s", to, subject)
        return {"backend": "smtp", "to": to}
    outbox = settings.data_dir / "outbox"
    outbox.mkdir(exist_ok=True)
    path = outbox / f"{time.time_ns()}.eml"
    path.write_bytes(bytes(msg))
    print(f"\n===== EMAIL to {to}: {subject} =====\n{body}\n===== ({path}) =====\n")
    return {"backend": "console", "to": to, "file": str(path)}
