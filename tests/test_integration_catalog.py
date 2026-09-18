from backend.app.modules.integrations.catalog import (
    compatible_integration_types,
    executable_integration_types,
    integration_packaged_operations,
    integration_requires_operation_endpoints,
)


def test_packaged_requirements_choose_only_their_connector():
    assert compatible_integration_types("email_send") == {"email_smtp"}
    assert compatible_integration_types("email_read") == {"email_imap"}
    assert compatible_integration_types("instagram_publish") == {"instagram_publish"}


def test_unknown_requirements_fall_back_only_to_generic_execution_connectors():
    compatible = compatible_integration_types("specialist_external_system")
    assert {"custom_api", "pos", "crm", "erp", "webhook"} <= compatible
    assert "calendar" not in compatible
    assert "email_smtp" not in compatible
    assert "email_imap" not in compatible
    assert "instagram_publish" not in compatible


def test_registry_declares_real_execution_and_endpoint_contracts():
    executable = executable_integration_types()
    assert {"email_smtp", "email_imap", "instagram_publish", "calendar"} <= executable
    assert {"custom_api", "pos", "crm", "erp", "webhook"} <= executable

    booking = compatible_integration_types("booking")
    assert "calendar" in booking
    assert "custom_api" in booking
    assert integration_packaged_operations("calendar") == {
        "availability": {"adapter": "google_calendar"},
        "execute": {"adapter": "google_calendar"},
        "cancel": {"adapter": "google_calendar"},
    }

    assert integration_requires_operation_endpoints("custom_api") is True
    assert integration_requires_operation_endpoints("crm") is True
    assert integration_requires_operation_endpoints("email_smtp") is False
    assert integration_requires_operation_endpoints("email_imap") is False
    assert integration_requires_operation_endpoints("instagram_publish") is False
    assert integration_requires_operation_endpoints("calendar") is False
