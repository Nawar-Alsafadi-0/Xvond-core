from backend.app.modules.ai_agent.employee_builder import (
    blueprint_readiness,
    build_employee_blueprint,
    runtime_channels_for,
    runtime_tools_for,
)


def test_arabic_business_description_builds_expected_blueprint():
    blueprint = build_employee_blueprint(
        "بدي موظف يرد على العملاء على واتساب وانستا، يبيع ويتابع العملاء ويحجز مواعيد"
    )

    assert blueprint.audience == "business"
    assert "customer_support" in blueprint.capabilities
    assert "sales" in blueprint.capabilities
    assert "booking" in blueprint.capabilities
    assert "whatsapp" in blueprint.channels
    assert "instagram" in blueprint.channels

    tools = runtime_tools_for(blueprint.capabilities)
    assert "lead" in tools
    assert "booking" in tools
    assert "human_handoff" in tools

    readiness = blueprint_readiness(blueprint)
    assert readiness["channels"]["whatsapp"] == "connect_required"
    assert readiness["channels"]["instagram"] == "planned"


def test_personal_employee_defaults_to_xvond_channel():
    blueprint = build_employee_blueprint(
        "بدي مساعد شخصي إلي يبحث بالانترنت ويرتب مهامي كل يوم"
    )

    assert blueprint.audience == "personal"
    assert "web_research" in blueprint.capabilities
    assert "scheduling" in blueprint.capabilities
    assert blueprint.channels == ("xvond",)
    assert runtime_channels_for(blueprint.channels) == ()


def test_unknown_description_still_creates_safe_general_employee():
    blueprint = build_employee_blueprint(
        "بدي موظف يساعدني بتنظيم شغلة خاصة فيني بطريقة مرتبة"
    )

    assert blueprint.capabilities
    assert blueprint.channels == ("xvond",)
    assert blueprint.permissions
