from backend.app.modules.integrations import email_smtp
from backend.app.modules.tools import action_request as runtime


class FakeSMTP:
    def __init__(self):
        self.logged_in = None
        self.messages = []
        self.quit_called = False

    def login(self, username, password):
        self.logged_in = (username, password)

    def send_message(self, message):
        self.messages.append(message)
        return {}

    def quit(self):
        self.quit_called = True


def _config():
    return {
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "username": "mailer@example.com",
        "password": "secret",
        "from_address": "mailer@example.com",
    }


def test_secure_smtp_send_uses_authenticated_connection(monkeypatch):
    client = FakeSMTP()
    monkeypatch.setattr(email_smtp, "_connection", lambda *args, **kwargs: client)

    result = email_smtp.send_smtp_email(
        config=_config(),
        to_address="customer@example.net",
        subject="Appointment",
        body="Your appointment is confirmed.",
    )

    assert result["accepted"] is True
    assert result["to"] == "customer@example.net"
    assert client.logged_in == ("mailer@example.com", "secret")
    assert len(client.messages) == 1
    assert client.messages[0]["Subject"] == "Appointment"
    assert client.quit_called is True


def test_smtp_rejects_private_or_reserved_hosts(monkeypatch):
    monkeypatch.setattr(
        email_smtp.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (email_smtp.socket.AF_INET, email_smtp.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
        ],
    )

    try:
        email_smtp._public_smtp_host("smtp.example.com")
    except email_smtp.EmailConnectorError as exc:
        assert "private or reserved" in str(exc)
    else:
        raise AssertionError("private SMTP host must be rejected")


def test_email_action_uses_stable_idempotency_claim(monkeypatch):
    sent = []

    monkeypatch.setattr(
        runtime,
        "send_smtp_email",
        lambda **kwargs: sent.append(kwargs) or {
            "provider": "smtp",
            "to": kwargs["to_address"],
            "from": "mailer@example.com",
            "accepted": True,
        },
    )

    first = runtime._email_send_call(
        config=_config(),
        payload={
            "details": {
                "to": "customer@example.net",
                "subject": "Hello",
                "body": "Message body",
            }
        },
        operation="execute",
        idempotency_key="email-test-unique-001",
    )
    second = runtime._email_send_call(
        config=_config(),
        payload={
            "details": {
                "to": "customer@example.net",
                "subject": "Hello",
                "body": "Message body",
            }
        },
        operation="execute",
        idempotency_key="email-test-unique-001",
    )

    assert first.success is True
    assert second.success is False
    assert second.data["reconciliation_required"] is True
    assert len(sent) == 1


def test_email_action_accepts_ai_generated_body(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        runtime,
        "send_smtp_email",
        lambda **kwargs: captured.update(kwargs) or {
            "provider": "smtp",
            "to": kwargs["to_address"],
            "from": "mailer@example.com",
            "accepted": True,
        },
    )

    result = runtime._email_send_call(
        config=_config(),
        payload={
            "details": {
                "to_email": "customer@example.net",
                "subject": "Follow-up",
                "ai_response": "Generated follow-up text",
            }
        },
        operation="execute",
        idempotency_key="email-test-ai-body-002",
    )

    assert result.success is True
    assert captured["body"] == "Generated follow-up text"
    assert captured["to_address"] == "customer@example.net"
