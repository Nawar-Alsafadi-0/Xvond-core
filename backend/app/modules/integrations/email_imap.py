from __future__ import annotations

import email
import imaplib
import ipaddress
import socket
import ssl
from email.header import decode_header, make_header
from email.policy import default


_ALLOWED_IMAP_PORTS = {993}
_MAX_MESSAGES = 20
_MAX_BODY_CHARS = 4000


class EmailReadConnectorError(ValueError):
    pass


def _public_imap_host(value: str) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    if not host or len(host) > 253:
        raise EmailReadConnectorError("IMAP host is invalid")
    if host in {"localhost", "localhost.localdomain"}:
        raise EmailReadConnectorError("IMAP host cannot be local")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise EmailReadConnectorError("IMAP host could not be resolved") from exc
    if not infos:
        raise EmailReadConnectorError("IMAP host could not be resolved")
    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise EmailReadConnectorError("IMAP host resolved to an invalid address") from exc
        if not ip.is_global:
            raise EmailReadConnectorError("IMAP host cannot resolve to private or reserved addresses")
    return host


def _port(value) -> int:
    try:
        port = int(value or 993)
    except (TypeError, ValueError) as exc:
        raise EmailReadConnectorError("IMAP port is invalid") from exc
    if port not in _ALLOWED_IMAP_PORTS:
        raise EmailReadConnectorError("IMAP port must be 993")
    return port


def _mailbox(value: str | None) -> str:
    mailbox = str(value or "INBOX").strip()
    if not mailbox or len(mailbox) > 200 or any(ch in mailbox for ch in "\r\n"):
        raise EmailReadConnectorError("IMAP mailbox is invalid")
    return mailbox


def _connect(config: dict, *, timeout: float):
    host = _public_imap_host(config.get("imap_host"))
    port = _port(config.get("imap_port"))
    context = ssl.create_default_context()
    return imaplib.IMAP4_SSL(
        host=host,
        port=port,
        ssl_context=context,
        timeout=timeout,
    )


def _login_and_select(client, config: dict) -> str:
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    if not username or not password:
        raise EmailReadConnectorError("IMAP username and password are required")
    client.login(username, password)
    mailbox = _mailbox(config.get("mailbox"))
    status, _ = client.select(mailbox, readonly=True)
    if str(status or "").upper() != "OK":
        raise EmailReadConnectorError("IMAP mailbox could not be opened read-only")
    return mailbox


def validate_imap_connection(config: dict, *, timeout: float = 10.0) -> dict:
    client = None
    try:
        client = _connect(config, timeout=timeout)
        mailbox = _login_and_select(client, config)
        return {
            "validated": True,
            "mode": "imap_ssl_read_only",
            "imap_host": _public_imap_host(config.get("imap_host")),
            "imap_port": _port(config.get("imap_port")),
            "mailbox": mailbox,
        }
    except EmailReadConnectorError:
        raise
    except (imaplib.IMAP4.error, OSError, TimeoutError) as exc:
        raise EmailReadConnectorError("IMAP authentication or TLS validation failed") from exc
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def _decoded_header(value) -> str:
    try:
        return str(make_header(decode_header(str(value or ""))))
    except Exception:
        return str(value or "")


def _body_text(message) -> str:
    try:
        part = message.get_body(preferencelist=("plain",))
    except Exception:
        part = None
    if part is not None:
        try:
            return str(part.get_content() or "")[:_MAX_BODY_CHARS]
        except Exception:
            return ""
    if not message.is_multipart():
        try:
            return str(message.get_content() or "")[:_MAX_BODY_CHARS]
        except Exception:
            payload = message.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload.decode("utf-8", errors="replace")[:_MAX_BODY_CHARS]
    return ""


def read_imap_messages(
    *,
    config: dict,
    unread_only: bool = True,
    limit: int = 10,
    timeout: float = 15.0,
) -> dict:
    safe_limit = max(1, min(int(limit or 10), _MAX_MESSAGES))
    client = None
    try:
        client = _connect(config, timeout=timeout)
        mailbox = _login_and_select(client, config)
        criterion = "UNSEEN" if unread_only else "ALL"
        status, data = client.search(None, criterion)
        if str(status or "").upper() != "OK":
            raise EmailReadConnectorError("IMAP search failed")
        ids = []
        if data and isinstance(data[0], (bytes, bytearray)):
            ids = data[0].split()
        selected = ids[-safe_limit:]
        messages = []
        for message_id in reversed(selected):
            status, fetched = client.fetch(message_id, "(BODY.PEEK[])")
            if str(status or "").upper() != "OK":
                continue
            raw = None
            for item in fetched or []:
                if (
                    isinstance(item, tuple)
                    and len(item) >= 2
                    and isinstance(item[1], (bytes, bytearray))
                ):
                    raw = bytes(item[1])
                    break
            if raw is None:
                continue
            parsed = email.message_from_bytes(raw, policy=default)
            messages.append(
                {
                    "imap_id": message_id.decode("ascii", errors="ignore"),
                    "message_id": str(parsed.get("Message-ID") or "")[:500],
                    "subject": _decoded_header(parsed.get("Subject"))[:1000],
                    "from": _decoded_header(parsed.get("From"))[:1000],
                    "to": _decoded_header(parsed.get("To"))[:1000],
                    "date": str(parsed.get("Date") or "")[:500],
                    "body_text": _body_text(parsed),
                }
            )
        return {
            "provider": "imap",
            "mailbox": mailbox,
            "unread_only": bool(unread_only),
            "messages": messages,
            "count": len(messages),
        }
    except EmailReadConnectorError:
        raise
    except (imaplib.IMAP4.error, OSError, TimeoutError) as exc:
        raise EmailReadConnectorError("IMAP read failed") from exc
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
