from pathlib import Path

from backend.app.core.company_catalog import company_catalog, normalize_business_type, normalize_country


def source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8-sig")


def test_company_catalog_contains_catering_and_normalizes_oman():
    catalog = company_catalog()
    assert "Catering / Events" in catalog["business_types"]
    assert normalize_business_type("catering") == "Catering / Events"
    assert normalize_country("عُمان") == "Oman"
    assert "OMR" in catalog["currencies"]
    assert "Asia/Muscat" in catalog["timezones"]


def test_website_conversations_require_visitor_token_not_only_id():
    public_api = source("backend/app/api/public_channels.py")
    widget = source("backend/app/api/website_widget.py")
    assert "X-Xvond-Visitor-Token" in source("backend/app/main.py")
    assert "visitor_token" in public_api
    assert "verify_website_visitor_token" in public_api
    assert "conversation_id" in public_api
    assert "xvond_visitor_" in widget
    assert "X-Xvond-Visitor-Token" in widget


def test_external_actions_have_durable_idempotency_and_reconciliation():
    runtime = source("backend/app/modules/tools/action_request.py")
    operations = source("backend/app/api/admin_operations.py")
    reconciliation = source("backend/app/modules/tools/external_reconciliation.py")
    assert "Idempotency-Key" in runtime
    assert 'state="executing"' in runtime
    assert 'state="external_failed"' in runtime
    assert "will not be retried automatically" in runtime
    assert "/requests/{request_id}/reconcile" in operations
    assert "reconcile_external_action_request" in operations
    assert '"not_executed"' in reconciliation
    assert "UNRESOLVED_EXTERNAL_STATUSES" in reconciliation


def test_automation_uses_safe_http_and_no_internal_chat_commit():
    runtime = source("backend/app/modules/automation/runtime.py")
    api = source("backend/app/api/admin_automation.py")
    assert "safe_http_request" in runtime
    assert "httpx.post" not in runtime
    assert "commit=False" in runtime
    assert "allow_tools=False" in runtime
    assert "secret" in api.lower()


def test_runtime_and_admin_are_off_legacy_billing_authority():
    runtime = source("backend/app/core/agent_runtime.py")
    readiness = source("backend/app/core/readiness.py")
    dashboard = source("backend/app/api/admin_dashboard.py")
    main = source("backend/app/main.py")
    assert "modules.billing.models" not in runtime
    assert "modules.billing.models" not in readiness
    assert "modules.billing.models" not in dashboard
    assert "admin_billing_router" not in main
    assert "admin_business_router" not in main
    assert not Path("backend/app/api/admin_billing.py").exists()
    assert not Path("backend/app/api/admin_business.py").exists()
    assert "ServiceSubscription" in readiness


def test_protected_business_information_cannot_be_created_toggled_or_deleted():
    knowledge = source("backend/app/api/admin_ai_employee_knowledge.py")
    assert "reserved for company-managed business knowledge" in knowledge
    assert "must remain enabled" in knowledge
    assert "Protected business setup knowledge cannot be deleted" in knowledge


def test_customer_portal_uses_generic_operations_only():
    customer = source("frontend/customer/app.js")
    business_api = source("backend/app/api/customer_business.py")
    assert "/customer/action-requests" in customer
    assert "/customer/business/leads" not in customer
    assert "/customer/business/bookings" not in customer
    assert "/customer/business/orders" not in customer
    assert '@router.get("/leads")' not in business_api
    assert '@router.get("/bookings")' not in business_api
    assert '@router.get("/orders")' not in business_api


def test_human_whatsapp_reply_calls_real_sender_before_db_sent_record():
    handoff = source("backend/app/api/admin_handoff.py")
    send_position = handoff.index("whatsapp_sender.send_text")
    message_position = handoff.index("AIMessage(conversation_id=conversation.id, role=\"human\"")
    assert send_position < message_position
    assert "WhatsApp delivery failed; the reply was not recorded as sent" in handoff


def test_admin_and_customer_logout_call_server_logout():
    assert 'api("/auth/logout"' in source("frontend/admin/app.js")
    assert 'api("/auth/logout"' in source("frontend/customer/session-security.js")


def test_production_acceptance_is_not_vendor_specific():
    acceptance = source("scripts/production_acceptance.py")
    assert 'checks["ai_providers"]' in acceptance
    assert 'checks["groq"]' not in acceptance


def test_live_ai_acceptance_does_not_create_customer_runtime_conversation():
    acceptance = source("scripts/production_acceptance.py")
    assert "ai_engine.generate" in acceptance
    assert "agent_runtime.chat" not in acceptance
    assert '"customer_runtime_used": False' in acceptance
    assert "tools=None" in acceptance
