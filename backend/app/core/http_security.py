
import ipaddress
import socket
from urllib.parse import urlparse

import httpx


BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
}

ALLOWED_SCHEMES = {
    "http",
    "https",
}


def _validate_ip(
    ip_value: str,
):

    ip = ipaddress.ip_address(
        ip_value
    )

    if not ip.is_global:
        raise ValueError(
            "Target IP is not publicly routable"
        )


def validate_public_http_url(
    url: str,
) -> str:

    if not isinstance(
        url,
        str,
    ):
        raise ValueError(
            "URL must be a string"
        )

    url = url.strip()

    if not url:
        raise ValueError(
            "URL is required"
        )

    parsed = urlparse(url)

    if (
        parsed.scheme.lower()
        not in ALLOWED_SCHEMES
    ):
        raise ValueError(
            "Only HTTP and HTTPS URLs are allowed"
        )

    if not parsed.hostname:
        raise ValueError(
            "URL hostname is required"
        )

    if (
        parsed.username
        or parsed.password
    ):
        raise ValueError(
            "Credentials inside URLs are not allowed"
        )

    hostname = (
        parsed.hostname
        .strip()
        .lower()
        .rstrip(".")
    )

    if (
        hostname in BLOCKED_HOSTS
        or hostname.endswith(
            ".localhost"
        )
        or hostname.endswith(
            ".local"
        )
        or hostname.endswith(
            ".internal"
        )
    ):
        raise ValueError(
            "Internal hostnames are not allowed"
        )

    try:

        # Literal IPv4 / IPv6.
        _validate_ip(
            hostname
        )

    except ValueError as literal_error:

        # If it looks like an IP but is blocked,
        # propagate the block.
        try:
            ipaddress.ip_address(
                hostname
            )

        except ValueError:
            pass

        else:
            raise literal_error

        # DNS hostname.
        try:

            resolved = (
                socket.getaddrinfo(
                    hostname,
                    parsed.port
                    or (
                        443
                        if parsed.scheme
                        == "https"
                        else 80
                    ),
                    type=socket.SOCK_STREAM,
                )
            )

        except socket.gaierror as exc:
            raise ValueError(
                "URL hostname could not be resolved"
            ) from exc

        if not resolved:
            raise ValueError(
                "URL hostname could not be resolved"
            )

        for item in resolved:

            ip_value = (
                item[4][0]
            )

            _validate_ip(
                ip_value
            )

    return url


def safe_http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    json_data=None,
    form_data: dict | None = None,
    multipart_data: dict | None = None,
    timeout: float = 15.0,
    max_response_bytes: int = 1_000_000,
) -> dict:

    url = validate_public_http_url(
        url
    )

    method = (
        method
        .strip()
        .upper()
    )

    if method not in {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }:
        raise ValueError(
            "Unsupported HTTP method"
        )

    timeout = max(
        1.0,
        min(
            float(timeout),
            30.0,
        ),
    )

    request_headers = {
        str(k): str(v)
        for k, v
        in (
            headers or {}
        ).items()
    }

    if form_data is not None and multipart_data is not None:
        raise ValueError("Form and multipart payloads are mutually exclusive")

    multipart_files = None
    if multipart_data is not None:
        multipart_files = {
            str(key): (None, str(value))
            for key, value in multipart_data.items()
            if value is not None
        }

    # Do not automatically follow redirects.
    # Prevents public URL -> private URL SSRF.
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
    ) as client:

        with client.stream(
            method,
            url,
            headers=request_headers,
            json=(
                json_data
                if form_data is None and multipart_data is None
                else None
            ),
            data=form_data,
            files=multipart_files,
        ) as response:

            body = bytearray()

            for chunk in (
                response.iter_bytes()
            ):

                remaining = (
                    max_response_bytes
                    - len(body)
                )

                if remaining <= 0:
                    break

                body.extend(
                    chunk[:remaining]
                )

            content = (
                bytes(body)
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )

            return {
                "status_code":
                    response.status_code,
                "response":
                    content,
                "truncated":
                    len(body)
                    >= max_response_bytes,
                "redirect_location":
                    response.headers.get(
                        "location"
                    ),
            }
