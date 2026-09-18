from backend.app.modules.ai_agent.employee_builder import (
    blueprint_readiness,
    build_employee_blueprint,
    missing_information_for,
    runtime_channels_for,
    runtime_tools_for,
    sanitize_channels,
)


def test_arabic_business_description_builds_expected_blueprint():
    blueprint = build_employee_blueprint(
        "بدي موظف يرد على العملاء على واتساب ورسائل انستا، يبيع ويتابع العملاء ويحجز مواعيد"
    )

    assert blueprint.name == "My AI Employee"
    assert blueprint.audience == "business"
    assert "customer_support" in blueprint.capabilities
    assert "sales" in blueprint.capabilities
    assert "booking" in blueprint.capabilities
    assert "whatsapp" in blueprint.channels
    assert "instagram" in blueprint.channels

    # Builder V1 must not attach legacy lead/booking/order tools. Those actions
    # are configured later through the canonical Business Actions system.
    tools = runtime_tools_for(blueprint.capabilities)
    assert tools == ("human_handoff",)

    readiness = blueprint_readiness(blueprint)
    assert readiness["capabilities"]["sales"] == "conversational_ready"
    assert readiness["capabilities"]["booking"] == "setup_required"
    assert readiness["channels"]["whatsapp"] == "connect_required"
    assert readiness["channels"]["instagram"] == "xvond_managed_setup"
    assert "business_actions" in blueprint.missing_information


def test_personal_employee_does_not_assume_a_channel():
    blueprint = build_employee_blueprint(
        "بدي مساعد شخصي إلي يبحث بالانترنت ويرتب مهامي كل يوم"
    )

    assert blueprint.name == "My AI Employee"
    assert blueprint.audience == "personal"
    assert "web_research" in blueprint.capabilities
    assert "scheduling" in blueprint.capabilities
    assert blueprint.channels == ()
    assert runtime_channels_for(blueprint.channels) == ()
    assert blueprint.permissions["scheduling"] == "ask_before_action"


def test_xvond_workspace_is_selected_only_when_requested():
    blueprint = build_employee_blueprint(
        "بدي مساعد شخصي إلي يضل داخل اكسفوند وينظم شغلي"
    )

    assert blueprint.audience == "personal"
    assert blueprint.channels == ("xvond",)
    assert blueprint_readiness(blueprint)["channels"]["xvond"] == "ready"


def test_unknown_description_becomes_custom_task_not_support_role():
    blueprint = build_employee_blueprint(
        "بدي موظف يساعدني بتنظيم شغلة خاصة فيني بطريقة مرتبة"
    )

    assert blueprint.name == "My AI Employee"
    assert blueprint.capabilities == ("custom_task",)
    assert blueprint.channels == ()
    assert blueprint.permissions == {"custom_task": "automatic"}
    assert blueprint_readiness(blueprint)["capabilities"]["custom_task"] == "conversational_ready"


def test_explicit_empty_channel_selection_is_preserved():
    assert sanitize_channels([], ("whatsapp",)) == ()


def test_requested_channels_are_not_created_as_runtime_placeholders():
    blueprint = build_employee_blueprint(
        "بدي موظف يرد على العملاء على واتساب وموقعنا"
    )

    assert "whatsapp" in blueprint.channels
    assert "website" in blueprint.channels
    assert runtime_channels_for(blueprint.channels) == ()
    assert "connect_whatsapp" in blueprint.missing_information
    assert "connect_website" in blueprint.missing_information


def test_setup_requirements_follow_final_user_selections():
    missing = missing_information_for(
        ("content", "scheduling"),
        ("xvond", "email"),
    )

    assert missing == ("automation", "connect_email")
    assert "knowledge" not in missing
    assert "business_actions" not in missing


def test_email_and_instagram_actions_do_not_implicitly_become_conversation_channels():
    email_worker = build_employee_blueprint(
        "بدي مساعد يقرأ إيميلاتي كل صباح ويلخص المهم ويجهز ردود بعد موافقتي"
    )
    assert "email" in email_worker.capabilities
    assert "email" not in email_worker.channels

    publisher = build_employee_blueprint(
        "بدي موظف يولد محتوى وينشره على انستغرام كل يوم"
    )
    assert "content" in publisher.capabilities
    assert "instagram" not in publisher.channels

    inbox = build_employee_blueprint(
        "بدي موظف يرد على العملاء من خلال رسائل انستا"
    )
    assert "instagram" in inbox.channels
