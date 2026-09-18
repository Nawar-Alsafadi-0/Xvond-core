from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.app.api.admin_analytics_builder import summarize_records
from backend.app.api.admin_service_billing import _validated_limits
from backend.app.api.public_channels import website_origin_allowed
from backend.app.modules.automation.runtime import automation_runtime
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.integrations.catalog import validate_integration_config
from backend.app.modules.billing.service_models import ServicePlan


class Record:
    def __init__(self, data):
        self.data = data


def test_analytics_summary_handles_numeric_and_categorical_values():
    summary = summarize_records(
        [
            Record({"sales": 10, "branch": "Muscat", "open": True}),
            Record({"sales": "20.5", "branch": "Muscat", "open": False}),
            Record({"sales": 4.5, "branch": "Sohar", "open": True}),
        ]
    )

    assert summary["record_count"] == 3
    assert summary["numeric_metrics"]["sales"]["count"] == 3
    assert summary["numeric_metrics"]["sales"]["sum"] == "35.0"
    assert summary["numeric_metrics"]["sales"]["min"] == "4.5"
    assert summary["numeric_metrics"]["sales"]["max"] == "20.5"
    assert summary["categories"]["branch"][0] == {"value": "Muscat", "count": 2}
    assert {x["value"]: x["count"] for x in summary["categories"]["open"]} == {
        "True": 2,
        "False": 1,
    }


def test_service_plan_limits_are_validated_and_normalized():
    limits = _validated_limits({"tokens": 10000, "agents": "3", "runs": Decimal("12.50")})
    assert limits == {"tokens": "10000", "agents": "3", "runs": "12.5"}

    with pytest.raises(HTTPException):
        _validated_limits({"tokens": -1})

    with pytest.raises(HTTPException):
        _validated_limits({"tokens": "not-a-number"})


def test_zero_service_limit_means_unlimited_but_negative_is_invalid():
    plan = ServicePlan(
        service_code="automation",
        tier="starter",
        name="Starter",
        monthly_price=0,
        currency="OMR",
        limits={"runs": 0},
        enabled=True,
    )
    assert service_limits.limit_value(plan, "runs") is None

    plan.limits = {"runs": -1}
    with pytest.raises(HTTPException):
        service_limits.limit_value(plan, "runs")


def test_ai_agent_limit_usage_counts_provisioned_agents():
    db = MagicMock()
    query = MagicMock()
    db.query.return_value = query
    query.filter.return_value = query
    query.scalar.return_value = 1
    subscription = SimpleNamespace(service_code="ai_agents", company_id=4)

    assert service_limits.used(db, subscription, "agents") == Decimal("1")
    query.scalar.assert_called_once()


def test_integration_config_requires_real_required_fields():
    assert validate_integration_config(
        "pos",
        {
            "base_url": "https://pos.example.com",
            "api_key": "secret",
            "validation_endpoint": "/account",
        },
    ) is True
    assert validate_integration_config(
        "webhook", {"url": "https://hooks.example.com/xvond"}
    ) is True

    with pytest.raises(ValueError, match="base_url"):
        validate_integration_config("crm", {})

    with pytest.raises(ValueError, match="validation_endpoint"):
        validate_integration_config(
            "crm", {"base_url": "https://crm.example.com"}
        )

    with pytest.raises(ValueError, match="Unsupported integration type"):
        validate_integration_config("unknown", {})


def test_website_origin_matching_does_not_allow_domain_spoofing():
    assert website_origin_allowed("https://example.com", "example.com") is True
    assert website_origin_allowed("https://www.example.com", "example.com") is True
    assert website_origin_allowed("https://chat.eu.example.com", "example.com") is True
    assert website_origin_allowed("https://example.com.evil.test", "example.com") is False
    assert website_origin_allowed("https://evil-example.com", "example.com") is False
    assert website_origin_allowed("", "example.com") is False


def test_automation_transform_and_condition_steps_are_deterministic():
    transformed = automation_runtime.execute_step(
        db=None,
        company_id=1,
        step={"type": "transform", "values": {"lead_score": 90}},
        state={},
        run_id=1,
        step_index=0,
    )
    assert transformed == {"lead_score": 90}

    passed = automation_runtime.execute_step(
        db=None,
        company_id=1,
        step={"type": "condition", "field": "lead_score", "equals": 90},
        state={"lead_score": 90},
        run_id=1,
        step_index=1,
    )
    assert passed == {"condition_passed": True}

    with pytest.raises(ValueError, match="Condition failed"):
        automation_runtime.execute_step(
            db=None,
            company_id=1,
            step={"type": "condition", "field": "lead_score", "equals": 90},
            state={"lead_score": 20},
            run_id=1,
            step_index=2,
        )

    with pytest.raises(ValueError, match="Unsupported automation step"):
        automation_runtime.execute_step(
            db=None,
            company_id=1,
            step={"type": "not-real"},
            state={},
            run_id=1,
            step_index=3,
        )
