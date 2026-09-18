from backend.app.modules.tools.bootstrap import register_builtin_tools
from backend.app.modules.tools.registry import tool_registry
from backend.app.modules.tools.workflow_action_request import WorkflowActionRequestTool


def test_action_request_registry_uses_workflow_engine_tool():
    register_builtin_tools()
    tool = tool_registry.get("action_request")
    assert isinstance(tool, WorkflowActionRequestTool)


def test_availability_is_sent_to_workflow_engine(monkeypatch):
    tool = WorkflowActionRequestTool()
    captured = {}

    def fake_execute(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "request_id": kwargs.get("request_id") or "generated",
            "data": {"available": True, "available_slots": ["17:00"]},
        }

    monkeypatch.setattr(
        "backend.app.modules.tools.workflow_action_request.n8n_gateway.execute",
        fake_execute,
    )

    result = tool.execute(
        {
            "operation": "check_availability",
            "action_type": "booking",
            "details": {"date": "2026-09-01"},
        },
        {
            "company_id": 12,
            "agent_id": 4,
            "conversation_id": 33,
            "config": {
                "actions": {
                    "booking": {
                        "enabled": True,
                        "destination": {"type": "integration", "integration_id": 7},
                        "availability": {"mode": "integration"},
                    }
                }
            },
        },
    )

    assert result.success is True
    assert captured["company_id"] == 12
    assert captured["agent_id"] == 4
    assert captured["conversation_id"] == 33
    assert captured["action"] == "booking.check_availability"
    assert captured["data"]["details"] == {"date": "2026-09-01"}
    assert result.data["available"] is True


def test_xvond_internal_action_bypasses_workflow_engine(monkeypatch):
    tool = WorkflowActionRequestTool()

    def fail_gateway(**kwargs):
        raise AssertionError("workflow engine must not be called for Xvond-native action")

    monkeypatch.setattr(
        "backend.app.modules.tools.workflow_action_request.n8n_gateway.execute",
        fail_gateway,
    )
    result = tool.execute(
        {
            "operation": "check_availability",
            "action_type": "booking",
            "details": {"date": "2026-09-20"},
        },
        {
            "company_id": 12,
            "agent_id": 4,
            "conversation_id": 33,
            "config": {
                "actions": {
                    "booking": {
                        "enabled": True,
                        "destination": {"type": "xvond_internal", "adapter": "booking"},
                        "availability": {
                            "mode": "xvond_schedule",
                            "date_field": "date",
                            "time_field": "time",
                            "schedule": {
                                "weekdays": [6],
                                "start": "09:00",
                                "end": "10:00",
                                "slot_minutes": 30,
                                "capacity": 1,
                            },
                        },
                    }
                }
            },
            "db": type("FakeDb", (), {
                "query": lambda self, *args, **kwargs: type("Q", (), {
                    "filter": lambda self, *args, **kwargs: self,
                    "order_by": lambda self, *args, **kwargs: self,
                    "limit": lambda self, *args, **kwargs: self,
                    "all": lambda self: [],
                })(),
            })(),
        },
    )
    assert result.success is True
    assert result.data["available_slots"] == ["09:00", "09:30"]
