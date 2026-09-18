from pathlib import Path

import pytest

from scripts import install_nginx_core_routes as installer


def test_public_base_host_comes_from_runtime_origin(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.xvond.com")
    assert installer._public_base_host() == "api.xvond.com"


@pytest.mark.parametrize(
    "value",
    [
        "http://api.xvond.com",
        "https://api.xvond.com/path",
        "https://api.xvond.com?bad=1",
        "https://user:pass@api.xvond.com",
    ],
)
def test_public_base_host_rejects_unsafe_values(monkeypatch, value):
    monkeypatch.setenv("PUBLIC_BASE_URL", value)
    with pytest.raises(installer.InstallError):
        installer._public_base_host()


def test_domain_matching_targets_requested_api_vhost_only():
    api = "server { server_name api.xvond.com; location / {} }"
    root = "server { server_name xvond.com www.xvond.com; location / {} }"
    assert installer._contains_domain_server(api, "api.xvond.com") is True
    assert installer._contains_domain_server(root, "api.xvond.com") is False
    assert installer._contains_domain_server(root, "xvond.com") is True


def test_install_include_places_core_routes_in_requested_vhost(tmp_path, monkeypatch):
    target = tmp_path / "site.conf"
    target.write_text(
        "server {\n"
        "    listen 443 ssl;\n"
        "    server_name xvond.com;\n"
        "    include /etc/nginx/snippets/xvond-core-locations.conf;\n"
        "    location / { return 200; }\n"
        "}\n\n"
        "server {\n"
        "    listen 443 ssl;\n"
        "    server_name api.xvond.com;\n"
        "    location /health-only { return 200; }\n"
        "}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(installer, "INCLUDE_PATH", Path("/etc/nginx/snippets/xvond-core-locations.conf"))
    monkeypatch.setattr(installer, "INCLUDE_LINE", "    include /etc/nginx/snippets/xvond-core-locations.conf;")

    _target, changed = installer._install_include(target, "api.xvond.com")
    rendered = target.read_text(encoding="utf-8")

    assert changed is True
    assert rendered.count("include /etc/nginx/snippets/xvond-core-locations.conf;") == 2
    api_start = rendered.index("server_name api.xvond.com;")
    include_at = rendered.index("include /etc/nginx/snippets/xvond-core-locations.conf;", api_start)
    assert include_at > api_start


def test_env_file_is_used_when_public_base_url_is_not_exported(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    (tmp_path / ".env").write_text("PUBLIC_BASE_URL=https://api.xvond.com\n", encoding="utf-8")
    monkeypatch.setattr(installer, "REPO_ROOT", tmp_path)
    assert installer._public_base_host() == "api.xvond.com"


def test_install_include_is_idempotent_inside_target_vhost(tmp_path, monkeypatch):
    target = tmp_path / "api.conf"
    target.write_text(
        "server {\n"
        "    server_name api.xvond.com;\n"
        "    include /etc/nginx/snippets/xvond-core-locations.conf;\n"
        "    location / { return 200; }\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        installer,
        "INCLUDE_PATH",
        Path("/etc/nginx/snippets/xvond-core-locations.conf"),
    )
    monkeypatch.setattr(
        installer,
        "INCLUDE_LINE",
        "    include /etc/nginx/snippets/xvond-core-locations.conf;",
    )

    before = target.read_text(encoding="utf-8")
    _target, changed = installer._install_include(target, "api.xvond.com")

    assert changed is False
    assert target.read_text(encoding="utf-8") == before
