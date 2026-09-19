from backend.app.modules.integrations import email_imap
from backend.app.modules.tools import action_request as runtime


RAW_MESSAGE = b"""From: sender@example.com\r\nTo: owner@example.com\r\nSubject: New lead\r\nMessage-ID: <msg-1@example.com>\r\nDate: Fri, 18 Sep 2026 10:00:00 +0000\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlease call me back.\r\n"""


class FakeIMAP:
    def __init__(self):
        self.login_args = None
        self.select_args = None
        self.fetch_calls = []
        self.logged_out = False

    def login(self, username, password):
        self.login_args = (username, password)
        return "OK", []

    def select(self, mailbox, readonly=False):
        self.select_args = (mailbox, readonly)
        return "OK", [b"2"]

    def search(self, charset, criterion):
        assert charset is None
        assert criterion == "UNSEEN"
        return "OK", [b"1 2"]

    def fetch(self, message_id, query):
        self.fetch_calls.append((message_id, query))
        return "OK", [(b"meta", RAW_MESSAGE)]

    def logout(self):
        self.logged_out = True
        return "BYE", []


def _config():
    return {
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "username": "owner@example.com",
        "password": "secret",
        "mailbox": "INBOX",
    }


def test_imap_reads_messages_without_marking_them_seen(monkeypatch):
    client = FakeIMAP()
    monkeypatch.setattr(email_imap, "_connect", lambda *args, **kwargs: client)

    result = email_imap.read_imap_messages(
        config=_config(),
        unread_only=True,
        limit=1,
    )

    assert result["count"] == 1
    assert result["messages"][0]["subject"] == "New lead"
    assert result["messages"][0]["body_text"].strip() == "Please call me back."
    assert client.select_args == ("INBOX", True)
    assert client.fetch_calls == [(b"2", "(BODY.PEEK[])")]
    assert client.logged_out is True


def test_imap_rejects_private_or_reserved_hosts(monkeypatch):
    monkeypatch.setattr(
        email_imap.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (email_imap.socket.AF_INET, email_imap.socket.SOCK_STREAM, 6, "", ("10.0.0.8", 0))
        ],
    )
    try:
        email_imap._public_imap_host("imap.example.com")
    except email_imap.EmailReadConnectorError as exc:
        assert "private or reserved" in str(exc)
    else:
        raise AssertionError("private IMAP host must be rejected")


def test_email_read_action_is_read_only_and_bounded(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        runtime,
        "read_imap_messages",
        lambda **kwargs: captured.update(kwargs) or {
            "provider": "imap",
            "mailbox": "INBOX",
            "unread_only": kwargs["unread_only"],
            "messages": [],
            "count": 0,
        },
    )

    result = runtime._email_read_call(
        config=_config(),
        payload={"details": {"unread_only": "false", "limit": 500}},
        operation="execute",
    )

    assert result.success is True
    assert captured["unread_only"] is False
    assert captured["limit"] == 500
