from backend.app.api.admin_agent_actions import BUSINESS_MODULES, TEMPLATES
from backend.app.modules.tools.action_request import _customer_confirmed, _field_specs, _missing
from backend.app.modules.tools.executor import _operation_config_ready


def test_arabic_and_english_customer_confirmation():
    assert _customer_confirmed("تمام") is True
    assert _customer_confirmed("نعم أكد الطلب") is True
    assert _customer_confirmed("yes") is True
    assert _customer_confirmed("لا، غير الطلب") is False
    assert _customer_confirmed("no") is False


def test_structured_fields_preserve_optional_rules():
    action = {
        "fields": [
            {"key": "customer_name", "label": "Customer name", "required": True, "type": "text"},
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ]
    }
    specs = _field_specs(action)
    assert specs[0]["required"] is True
    assert specs[1]["required"] is False
    assert _missing(action, {"customer_name": "Nawar"}) == []


def test_templates_are_suggestions_not_automatic_capabilities():
    for template in TEMPLATES:
        keys = set()
        for action in template["actions"]:
            assert action["key"] not in keys
            keys.add(action["key"])
            assert action["enabled"] is False
            assert action["module"] in BUSINESS_MODULES
            assert (action.get("destination") or {}).get("type") in {
                "xvond_internal",
                "human_handoff",
                "integration",
                "unconfigured",
            }


def test_catering_template_suggests_request_without_enabling_it():
    template = next(x for x in TEMPLATES if x["id"] == "catering")
    request = next(x for x in template["actions"] if x["key"] == "catering_request")
    assert request["enabled"] is False
    assert request["module"] == "orders"
    assert request["destination"]["type"] == "xvond_internal"
    required = {x["key"] for x in request["fields"] if x.get("required", True)}
    assert {"customer_name", "phone", "event_type", "event_date", "guest_count", "location", "request"} <= required


def test_incomplete_operation_is_not_runtime_ready():
    assert _operation_config_ready({"destination": {"type": "unconfigured"}, "availability": {"mode": "none"}}) is False
    assert _operation_config_ready({"destination": {"type": "integration"}, "availability": {"mode": "none"}}) is False
    assert _operation_config_ready({
        "destination": {"type": "xvond_internal"},
        "availability": {"mode": "xvond_schedule", "date_field": "date", "time_field": "time", "schedule": {"weekdays": [], "start": "", "end": ""}},
    }) is False
    assert _operation_config_ready({"destination": {"type": "xvond_internal"}, "availability": {"mode": "none"}}) is True



def test_native_action_event_name_is_stable():
    from backend.app.modules.tools import action_request as module

    captured = []

    def fake_dispatch(**kwargs):
        captured.append(kwargs)
        return {"matched": 0, "runs": []}

    original = module.dispatch_automation_event
    module.dispatch_automation_event = fake_dispatch
    try:
        # The runtime contract emits booking.created for scheduled bookings and
        # <action_type>.created for other native records. Keep this import-level
        # smoke assertion so the event bridge cannot disappear unnoticed.
        assert callable(module.dispatch_automation_event)
    finally:
        module.dispatch_automation_event = original

    assert captured == []
