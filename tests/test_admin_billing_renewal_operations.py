import inspect

from backend.app.api import admin_operations


def test_unresolved_billing_renewal_view_is_metadata_only():
    source = inspect.getsource(admin_operations._renewal_attempt_metadata)
    assert '"company_id"' in source
    assert '"service_subscription_id"' in source
    assert '"provider_transaction_id"' in source
    assert '"last_error_code"' in source
    assert "provider_customer_token" not in source
    assert "provider_card_token" not in source
    assert "payment_agreement_token" not in source
    assert "provider_config" not in source


def test_unresolved_billing_renewals_are_bounded_and_operator_only():
    source = inspect.getsource(admin_operations.unresolved_billing_renewals)
    signature = inspect.signature(admin_operations.unresolved_billing_renewals)
    assert "require_xvond_operator" in str(signature)
    assert "min(int(limit or 100), 500)" in source
    assert 'ServiceRenewalAttempt.status.in_(("sending", "submitted", "unknown", "failed"))' in source
