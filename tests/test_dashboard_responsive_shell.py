from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8-sig")


def test_customer_dashboard_has_accessible_mobile_navigation():
    html = _read("frontend/customer/index.html")
    javascript = _read("frontend/customer/app.js")
    css = _read("frontend/customer/responsive-shell.css")

    assert 'id="customer-sidebar"' in html
    assert 'id="customer-menu-toggle"' in html
    assert 'aria-controls="customer-sidebar"' in html
    assert 'aria-expanded="false"' in html
    assert 'id="customer-nav-scrim"' in html
    assert 'href="#customer-main"' in html
    assert 'responsive-shell.css?v=' in html
    assert 'app.js?v=20260920-final1' in html

    assert "function setCustomerNavigationOpen" in javascript
    assert 'event.key === "Escape"' in javascript
    assert 'matchMedia("(max-width: 720px)")' in javascript
    assert "setCustomerNavigationOpen(false);" in javascript

    assert "body.customer-nav-open .portal .sidebar" in css
    assert "pointer-events: auto" in css
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css


def test_admin_dashboard_has_accessible_mobile_navigation_and_no_default_credential():
    html = _read("frontend/admin/index.html")
    javascript = _read("frontend/admin/app.js")
    css = _read("frontend/admin/responsive-shell.css")

    assert 'id="admin-sidebar"' in html
    assert 'id="admin-menu-toggle"' in html
    assert 'aria-controls="admin-sidebar"' in html
    assert 'aria-expanded="false"' in html
    assert 'id="admin-nav-scrim"' in html
    assert 'href="#admin-main"' in html
    assert 'autocomplete="username"' in html
    assert 'autocomplete="current-password"' in html
    assert 'value="admin@xvond.com"' not in html
    assert 'responsive-shell.css?v=' in html
    assert 'app.js?v=20260920-final1' in html

    assert "function setAdminNavigationOpen" in javascript
    assert 'event.key==="Escape"' in javascript
    assert 'matchMedia("(max-width: 900px)")' in javascript
    assert "setAdminNavigationOpen(false);" in javascript

    assert "body.admin-nav-open .app .sidebar" in css
    assert "pointer-events: auto" in css
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css


def test_dashboard_login_feedback_is_announced_to_assistive_technology():
    for relative_path in ("frontend/customer/index.html", "frontend/admin/index.html"):
        html = _read(relative_path)
        assert 'id="login-error"' in html
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html
