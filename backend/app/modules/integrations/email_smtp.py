from __future__ import annotations

import ipaddress
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import parseaddr


_ALLOWED_SMTP_PORTS = {465, 587}
_MAX_SUBJECT = 500
_MAX_BODY = 100_000


class EmailConnectorError(ValueError):
    pass


def _public_smtp_target(value: str, port: int) -> tuple[str, list[str]]:
    host = str(value or "").strip().lower().rstrip(".")
    if not host or len(host) > 253:
        raise EmailConnectorError("SMTP host is invalid")
    if host in {"localhost", "localhost.localdomain"}:
        raise EmailConnectorError("SMTP host cannot be local")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise EmailConnectorError("SMTP host could not be resolved") from exc
    if not infos:
        raise EmailConnectorError("SMTP host could not be resolved")

    addresses: list[str] = []
    for info in infos:
        raw_ip = str(info[4][0])
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise EmailConnectorError("SMTP host resolved to an invalid address") from exc
        if not ip.is_global:
            raise EmailConnectorError("SMTP host cannot resolve to private or reserved addresses")
        normalized = str(ip)
        if normalized not in addresses:
            addresses.append(normalized)
    return host, addresses


def _public_smtp_host(value: str) -> str:
    host, _ = _public_smtp_target(value, 465)
    return host


def _email_address(value: str, *, field: str) -> str:
    raw = str(value or "").strip()
    _, address = parseaddr(raw)
    if (
        not address
        or "@" not in address
        or address.count("@") != 1
        or any(ch in address for ch in "\r\n")
        or len(address) > 320
    ):
        raise EmailConnectorError(f"{field} is invalid")
    return address


def _port(value) -> int:
    try:
        port = int(value or 465)
    except (TypeError, ValueError) as exc:
        raise EmailConnectorError("SMTP port is invalid") from exc
    if port not in _ALLOWED_SMTP_PORTS:
        raise EmailConnectorError("SMTP port must be 465 or 587")
    return port


def _connection(config: dict, *, timeout: float):
    port = _port(config.get("smtp_port"))
    host, addresses = _public_smtp_target(config.get("smtp_host"), port)
    context = ssl.create_default_context()
    last_error: Exception | None = None

    for address in addresses:
        raw_socket = None
        client = None
        try:
            raw_socket = socket.create_connection((address, port), timeout=timeout)
            if port == 465:
                client = smtplib.SMTP_SSL(timeout=timeout, context=context)
                client._host = host
                client.sock = context.wrap_socket(raw_socket, server_hostname=host)
                raw_socket = None
                code, message = client.getreply()
                if code != 220:
                    raise smtplib.SMTPConnectError(code, message)
            else:
                client = smtplib.SMTP(timeout=timeout)
                client._host = host
                client.sock = raw_socket
                client.file = None
                raw_socket = None
                code, message = client.getreply()
                if code != 220:
                    raise smtplib.SMTPConnectError(code, message)
            client.ehlo()
            if port == 587:
                client.starttls(context=context)
                client.ehlo()
            return client
        except Exception as exc:
            last_error = exc
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            if raw_socket is not None:
                try:
                    raw_socket.close()
                except Exception:
                    pass

    raise EmailConnectorError("SMTP public endpoints could not be reached") from last_error


def validate_smtp_connection(config: dict, *, timeout: float = 10.0) -> dict:
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    from_address = _email_address(config.get("from_address"), field="From address")
    if not username or not password:
        raise EmailConnectorError("SMTP username and password are required")

    client = None
    try:
        client = _connection(config, timeout=timeout)
        client.login(username, password)
        return {
            "validated": True,
            "mode": "smtp_tls_authenticated",
            "smtp_host": _public_smtp_host(config.get("smtp_host")),
            "smtp_port": _port(config.get("smtp_port")),
            "from_address": from_address,
        }
    except EmailConnectorError:
        raise
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise EmailConnectorError("SMTP authentication or TLS validation failed") from exc
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass


def send_smtp_email(
    *,
    config: dict,
    to_address: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
    timeout: float = 15.0,
) -> dict:
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    if not username or not password:
        raise EmailConnectorError("SMTP username and password are required")

    sender = _email_address(config.get("from_address"), field="From address")
    recipient = _email_address(to_address, field="Recipient")
    clean_subject = str(subject or "").replace("\r", " ").replace("\n", " ").strip()
    clean_body = str(body or "")
    if not clean_subject:
        raise EmailConnectorError("Email subject is required")
    if len(clean_subject) > _MAX_SUBJECT:
        raise EmailConnectorError("Email subject is too long")
    if not clean_body.strip():
        raise EmailConnectorError("Email body is required")
    if len(clean_body) > _MAX_BODY:
        raise EmailConnectorError("Email body is too long")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = clean_subject
    if reply_to:
        message["Reply-To"] = _email_address(reply_to, field="Reply-To address")
    message.set_content(clean_body)

    client = None
    try:
        client = _connection(config, timeout=timeout)
        client.login(username, password)
        refused = client.send_message(message)
        if refused:
            raise EmailConnectorError("SMTP server refused one or more recipients")
        return {
            "provider": "smtp",
            "to": recipient,
            "from": sender,
            "accepted": True,
        }
    except EmailConnectorError:
        raise
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise EmailConnectorError("SMTP send failed") from exc
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass
