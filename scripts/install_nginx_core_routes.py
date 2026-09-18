#!/usr/bin/env python3
"""Install Xvond Core routes into the nginx vhost for PUBLIC_BASE_URL safely.

This script is intentionally conservative: it discovers the active nginx file
from ``nginx -T``, refuses ambiguous targets, writes a timestamped backup,
installs the version-controlled Core location snippet, validates with
``nginx -t`` and automatically restores the previous vhost if validation fails.

Run as root from the repository checkout:
    python3 scripts/install_nginx_core_routes.py
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse
from datetime import datetime, UTC

INCLUDE_PATH = Path("/etc/nginx/snippets/xvond-core-locations.conf")
INCLUDE_LINE = f"    include {INCLUDE_PATH};"
REPO_ROOT = Path(__file__).resolve().parents[1]
SNIPPET_SOURCE = REPO_ROOT / "ops" / "nginx" / "core-locations.conf"
FILE_MARKER = re.compile(r"^# configuration file (.+):$")


class InstallError(RuntimeError):
    pass


def _env_value(name: str) -> str:
    direct = os.getenv(name)
    if direct is not None:
        return direct.strip()

    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return ""
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return ""


def _public_base_host() -> str:
    raw = _env_value("PUBLIC_BASE_URL")
    if not raw:
        raise InstallError("PUBLIC_BASE_URL is required in .env or the process environment")
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise InstallError("PUBLIC_BASE_URL must be a valid HTTPS origin")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise InstallError(
            "PUBLIC_BASE_URL must not contain credentials, path, query or fragment"
        )
    return parsed.hostname.lower().strip(".")

def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _nginx_files() -> dict[Path, str]:
    output = _run("nginx", "-T").stdout
    files: dict[Path, list[str]] = {}
    current: Path | None = None
    for line in output.splitlines(keepends=True):
        marker = FILE_MARKER.match(line.rstrip("\n"))
        if marker:
            current = Path(marker.group(1)).resolve()
            files.setdefault(current, [])
            continue
        if current is not None:
            files[current].append(line)
    return {path: "".join(lines) for path, lines in files.items()}


def _contains_domain_server(text: str, domain: str) -> bool:
    escaped = re.escape(domain)
    return bool(re.search(rf"\bserver_name\s+[^;]*\b{escaped}\b[^;]*;", text))


def _discover_target(domain: str) -> Path:
    candidates = [
        path
        for path, text in _nginx_files().items()
        if _contains_domain_server(text, domain)
        and path.exists()
        and path.is_file()
        and not str(path).startswith("/etc/nginx/snippets/")
    ]
    unique = sorted(set(candidates))
    if len(unique) != 1:
        rendered = ", ".join(str(path) for path in unique) or "none"
        raise InstallError(
            f"Expected exactly one active nginx file containing server_name {domain}; found: {rendered}"
        )
    return unique[0]


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0]


def _server_bounds(lines: list[str], domain: str) -> tuple[int, int]:
    domain_line = None
    for index, line in enumerate(lines):
        if _contains_domain_server(_strip_comment(line), domain):
            domain_line = index
            break
    if domain_line is None:
        raise InstallError(f"Could not locate server_name {domain} in target file")

    start = None
    depth = 0
    for index in range(domain_line, -1, -1):
        code = _strip_comment(lines[index])
        if "server" in code and re.search(r"\bserver\s*\{", code):
            start = index
            break
    if start is None:
        raise InstallError("Could not locate opening server block")

    for index in range(start, len(lines)):
        code = _strip_comment(lines[index])
        depth += code.count("{")
        depth -= code.count("}")
        if depth == 0 and index > start:
            return start, index
    raise InstallError("Could not locate closing server block")


def _install_include(target: Path, domain: str) -> tuple[Path, bool]:
    original = target.read_text(encoding="utf-8")
    if str(INCLUDE_PATH) in original:
        return target, False

    lines = original.splitlines(keepends=True)
    start, end = _server_bounds(lines, domain)
    insertion = end
    for index in range(start + 1, end):
        code = _strip_comment(lines[index])
        if re.search(r"^\s*location\b", code):
            insertion = index
            break

    lines.insert(
        insertion,
        "\n    # Xvond Core control/customer/API routes (managed by repository script)\n"
        + INCLUDE_LINE
        + "\n",
    )
    target.write_text("".join(lines), encoding="utf-8")
    return target, True


def main() -> int:
    if os.geteuid() != 0:
        print("ERROR: run this script as root (sudo).", file=sys.stderr)
        return 2
    if not SNIPPET_SOURCE.exists():
        print(f"ERROR: missing repository snippet: {SNIPPET_SOURCE}", file=sys.stderr)
        return 2

    domain = _public_base_host()
    target = _discover_target(domain)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(f"{target.name}.xvond-backup-{timestamp}")
    shutil.copy2(target, backup)

    INCLUDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SNIPPET_SOURCE, INCLUDE_PATH)
    _, changed = _install_include(target, domain)

    validation = _run("nginx", "-t", check=False)
    if validation.returncode != 0:
        shutil.copy2(backup, target)
        print(validation.stdout, file=sys.stderr)
        print(f"ERROR: nginx validation failed; restored {backup}", file=sys.stderr)
        return 1

    reload_result = _run("systemctl", "reload", "nginx", check=False)
    if reload_result.returncode != 0:
        shutil.copy2(backup, target)
        _run("nginx", "-t", check=False)
        _run("systemctl", "reload", "nginx", check=False)
        print(reload_result.stdout, file=sys.stderr)
        print("ERROR: nginx reload failed; previous vhost restored.", file=sys.stderr)
        return 1

    print(f"OK: Xvond Core nginx routes {'installed' if changed else 'already included'}")
    print(f"Public API host: {domain}")
    print(f"Target: {target}")
    print(f"Backup: {backup}")
    print(f"Snippet: {INCLUDE_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
