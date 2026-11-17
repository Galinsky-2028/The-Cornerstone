"""The HTTPS email-API backend: provider formats, failures, no header injection, and safe fallback."""
import json

import httpx
import pytest

from foundation import mail
from foundation.config import settings


@pytest.fixture()
def http_backend(monkeypatch):
    monkeypatch.setattr(settings, "email_backend", "http")
    monkeypatch.setattr(settings, "email_api_url", "https://api.resend.com/emails")
    monkeypatch.setattr(settings, "email_api_key", "re_test_key")
    monkeypatch.setattr(settings, "email_from", "Example <no-reply@example.org>")
    monkeypatch.setattr(settings, "email_api_style", "")
    return monkeypatch


def client_returning(status=200, body=None, seen=None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"id": "msg_123"})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_resend_style_request(http_backend, tmp_path):
    seen = []
    att = tmp_path / "notice.txt"
    att.write_text("hello")
    r = mail.send_email("a@example.org", "Hi\r\nBcc: evil@example.org", "Body", html="<p>Body</p>", attachments=[att],
                        reply_to="r@example.org", http_client=client_returning(seen=seen))
    assert r == {"backend": "http", "style": "resend", "to": "a@example.org", "receipt": "msg_123"}
    req = seen[0]
    assert str(req.url) == "https://api.resend.com/emails" and req.headers["authorization"] == "Bearer re_test_key"
    body = json.loads(req.content)
    assert body["to"] == ["a@example.org"] and body["html"] == "<p>Body</p>" and body["reply_to"] == "r@example.org"
    assert "\n" not in body["subject"] and "\r" not in body["subject"]  # no header injection
    assert body["attachments"][0]["filename"] == "notice.txt"


def test_postmark_and_sendgrid_styles(http_backend):
    for url, style, header in [("https://api.postmarkapp.com/email", "postmark", "x-postmark-server-token"),
                               ("https://api.sendgrid.com/v3/mail/send", "sendgrid", "authorization")]:
        http_backend.setattr(settings, "email_api_url", url)
        seen = []
        r = mail.send_email("a@example.org", "S", "B", http_client=client_returning(status=202, body={}, seen=seen))
        assert r["style"] == style and header in seen[0].headers
        body = json.loads(seen[0].content)
        if style == "postmark":
            assert body["To"] == "a@example.org" and body["TextBody"] == "B"
        else:
            assert body["personalizations"][0]["to"][0]["email"] == "a@example.org"
            assert body["from"] == {"email": "no-reply@example.org", "name": "Example"}
    assert mail.api_style("https://mail.example.com/send", "") == "resend"
    with pytest.raises(ValueError):
        mail.api_style("https://x", "mailgun")


def test_provider_refusal_and_network_errors_raise_without_the_key(http_backend):
    with pytest.raises(mail.EmailError) as e:
        mail.send_email("a@example.org", "S", "B", http_client=client_returning(status=401, body={"error": "bad key"}))
    assert "401" in str(e.value) and "re_test_key" not in str(e.value)

    def boom(request):
        raise httpx.ConnectError("no route")
    with pytest.raises(mail.EmailError):
        mail.send_email("a@example.org", "S", "B", http_client=httpx.Client(transport=httpx.MockTransport(boom)))


def test_redirects_are_not_followed(http_backend):
    def redirect(request):
        return httpx.Response(302, headers={"location": "https://evil.example/collect"})
    with pytest.raises(mail.EmailError):
        mail.send_email("a@example.org", "S", "B", http_client=httpx.Client(transport=httpx.MockTransport(redirect)))


def test_incomplete_or_insecure_http_config_falls_back_to_console(http_backend):
    http_backend.setattr(settings, "email_api_key", "")
    assert mail.send_email("a@example.org", "S", "B")["backend"] == "console"
    http_backend.setattr(settings, "email_api_key", "k")
    http_backend.setattr(settings, "email_api_url", "http://api.resend.com/emails")
    assert mail.send_email("a@example.org", "S", "B")["backend"] == "console"


def test_password_reset_page_is_identical_when_the_provider_fails(client, monkeypatch):
    from conftest import csrf

    monkeypatch.setattr(settings, "email_backend", "http")
    monkeypatch.setattr(settings, "email_api_url", "https://127.0.0.1:9/unreachable")
    monkeypatch.setattr(settings, "email_api_key", "k")
    monkeypatch.setattr(settings, "email_from", "no-reply@example.org")
    token = csrf(client, "/reset")
    known = client.post("/reset", data={"email": "admin@example.com", "csrf": token})
    unknown = client.post("/reset", data={"email": "nobody@example.com", "csrf": token})
    assert known.status_code == unknown.status_code == 200
