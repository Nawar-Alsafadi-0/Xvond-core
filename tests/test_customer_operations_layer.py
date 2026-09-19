from backend.app.main import app
from backend.app.modules.customer_ops.models import (
    CustomerRecord,
    NotificationEvent,
    NotificationPreference,
)
from backend.app.modules.customer_ops.service import DEFAULT_EVENTS, identity_key


def test_customer_operations_routes_are_tenant_scoped_and_not_admin_mounted():
    paths = set(app.openapi()["paths"])
    assert "/customer/operations/customers" in paths
    assert "/customer/operations/notifications" in paths
    assert "/customer/operations/analytics" in paths
    assert not any(path.startswith("/admin/customer-operations") for path in paths)
    assert not any(path.startswith("/admin/admin/customer-operations") for path in paths)


def test_customer_identity_prefers_stable_contact_fields():
    assert (
        identity_key(phone="+968 99 123 456", email="a@example.com", external="abc")
        == "phone:+96899123456"
    )
    assert identity_key(email="A@Example.com") == "email:a@example.com"
    assert identity_key(external=" Visitor-10 ") == "external:visitor-10"
    assert (
        identity_key(external="+968 99 123 456", channel="whatsapp")
        == "phone:+96899123456"
    )


def test_notification_defaults_cover_business_and_runtime_attention():
    assert set(DEFAULT_EVENTS) == {
        "booking_new",
        "order_new",
        "lead_new",
        "handoff_pending",
        "operation_attention",
        "ai_failure",
        "employee_update",
    }


def test_customer_operations_models_are_registered():
    assert CustomerRecord.__tablename__ == "customer_records"
    assert NotificationPreference.__tablename__ == "notification_preferences"
    assert NotificationEvent.__tablename__ == "notification_events"
