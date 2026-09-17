from pathlib import Path

from backend.app.modules.ai_agent.employee_compiler import (
    build_compiler_user_message,
    parse_compiler_response,
)


ROOT = Path(__file__).resolve().parents[1]


def test_compiler_preserves_unknown_requirements_as_custom_work():
    job_brief = "راقب منصة متخصصة خاصة فيني ونفذ قاعدة مخصصة ما عنا تكامل جاهز إلها"
    response = """{
      "role": "Personal monitoring agent",
      "scope": "personal",
      "summary": "Monitor a private specialist platform and apply a custom rule.",
      "tasks": [{"name":"Monitor","description":"Watch the platform","trigger":"daily"}],
      "requirements": [
        {"key":"specialist_private_platform","kind":"custom","purpose":"Read private platform data"},
        {"key":"scheduling","kind":"automation","purpose":"Run daily"}
      ],
      "permissions": [{"action":"send a notification","mode":"automatic"}],
      "setup_questions": ["How should Xvond authenticate to the private platform?"]
    }"""
    spec = parse_compiler_response(response, job_brief=job_brief)

    assert spec["job_brief"] == job_brief
    custom = next(x for x in spec["requirements"] if x["key"] == "specialist_private_platform")
    assert custom["known_to_xvond"] is False
    assert custom["status"] == "custom_required"
    scheduling = next(x for x in spec["requirements"] if x["key"] == "scheduling")
    assert scheduling["known_to_xvond"] is True
    assert scheduling["status"] == "setup_required"


def test_compiler_maps_email_and_instagram_to_real_connection_requirements():
    response = """{
      "role": "Content and inbox employee",
      "scope": "business",
      "summary": "Read email and publish approved content.",
      "tasks": [],
      "requirements": [
        {"key":"email_read","kind":"integration","purpose":"Read inbox"},
        {"key":"email_send","kind":"integration","purpose":"Reply to email"},
        {"key":"instagram_publish","kind":"integration","purpose":"Publish content"}
      ],
      "permissions": [{"action":"publish content","mode":"ask_before"}],
      "setup_questions": []
    }"""
    spec = parse_compiler_response(response, job_brief="اقرأ الإيميل وانشر المحتوى")

    statuses = {x["key"]: x["status"] for x in spec["requirements"]}
    assert statuses["email_read"] == "connection_required"
    assert statuses["email_send"] == "connection_required"
    assert statuses["instagram_publish"] == "connection_required"


def test_compiler_prompt_keeps_full_job_and_selected_channels():
    message = build_compiler_user_message(
        job_brief="بدي موظف يرد عالعملاء ويحجز وينشر محتوى تلقائي",
        requested_channels=["whatsapp", "instagram"],
    )
    assert "يرد عالعملاء ويحجز وينشر محتوى تلقائي" in message
    assert "whatsapp, instagram" in message


def test_paid_compile_endpoint_is_part_of_customer_employee_builder_contract():
    source = (ROOT / "backend" / "app" / "api" / "customer_employee_builder.py").read_text(
        encoding="utf-8"
    )
    assert '@router.post("/{agent_id}/compile")' in source
    assert 'service_limits.entitlement(db, current_user.company_id, "ai_agents")' in source
    assert "_compile_employee_spec" in source
    assert 'builder["compiled_spec"] = compiled_spec' in source
    assert 'if not isinstance(builder.get("compiled_spec"), dict):' in source
