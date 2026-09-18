import inspect

from backend.app.api import admin_operations


def test_managed_delivery_metadata_excludes_customer_payload():
    source = inspect.getsource(admin_operations._managed_delivery_metadata)
    assert '"conversation_id"' in source
    assert '"status"' in source
    assert '"attempts"' in source
    assert '"last_error_code"' in source
    assert '"content"' not in source
    assert '"external_contact_id"' not in source
    assert '"inbound_external_message_id"' not in source


def test_unknown_managed_delivery_cannot_be_blindly_retried():
    source = inspect.getsource(admin_operations.retry_managed_channel_delivery)
    assert 'if row.status == "unknown"' in source
    assert "must be reconciled before any resend" in source
    assert 'row.status != "failed" or not row.retryable' in source
    assert "attempt_managed_delivery(" in source


def test_managed_delivery_reconciliation_requires_provider_identity_for_sent():
    source = inspect.getsource(admin_operations.reconcile_managed_channel_delivery)
    assert 'outcome not in {"sent", "not_sent"}' in source
    assert 'if row.status != "unknown"' in source
    assert "Provider message id is required" in source
    assert 'row.status = "accepted"' in source
    assert 'row.status = "failed"' in source
    assert "reconciled_not_sent" in source
    assert "mark_customer_roundtrip" in source


def test_managed_delivery_operations_are_admin_only_and_audited():
    retry_signature = inspect.signature(admin_operations.retry_managed_channel_delivery)
    reconcile_signature = inspect.signature(admin_operations.reconcile_managed_channel_delivery)
    assert "require_xvond_admin" in str(retry_signature)
    assert "require_xvond_admin" in str(reconcile_signature)

    retry_source = inspect.getsource(admin_operations.retry_managed_channel_delivery)
    reconcile_source = inspect.getsource(admin_operations.reconcile_managed_channel_delivery)
    assert 'action="managed_channel.delivery_retry_requested"' in retry_source
    assert 'action="managed_channel.delivery_retry_completed"' in retry_source
    assert 'action="managed_channel.delivery_reconciled"' in reconcile_source


def test_unresolved_managed_delivery_listing_is_bounded():
    source = inspect.getsource(admin_operations.unresolved_managed_channel_deliveries)
    assert "min(int(limit or 100), 500)" in source
    assert "ManagedChannelOutboundDelivery.status.in_(UNRESOLVED_DELIVERY)" in source
