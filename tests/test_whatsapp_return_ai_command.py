import inspect

from backend.app.api import whatsapp_webhook
from backend.app.api.whatsapp_webhook import _is_return_to_ai_command


def test_return_to_ai_command_accepts_explicit_control_phrases_only():
    assert _is_return_to_ai_command("/ai") is True
    assert _is_return_to_ai_command(" /RESUME-AI ") is True
    assert _is_return_to_ai_command("رجّع AI") is True
    assert _is_return_to_ai_command("ارجع الذكاء الاصطناعي") is True

    assert _is_return_to_ai_command("الـ ai ممتاز") is False
    assert _is_return_to_ai_command("رجعلي تفاصيل الطلب") is False


def test_business_app_command_resumes_ai_and_closes_handoff_without_mirroring():
    source = inspect.getsource(whatsapp_webhook.process_business_app_echo)

    command_pos = source.index("_is_return_to_ai_command(content)")
    resume_pos = source.index("resume_ai(session", command_pos)
    complete_pos = source.index("_complete_active_handoffs(", command_pos)
    commit_pos = source.index("db.commit()", command_pos)
    clear_marker_pos = source.index("clear_human_marker(", command_pos)
    continue_pos = source.index("continue", command_pos)
    normal_handoff_pos = source.index("activate_human_handoff(", continue_pos)

    assert command_pos < complete_pos < resume_pos < commit_pos < clear_marker_pos < continue_pos
    assert continue_pos < normal_handoff_pos
    assert '"status": "ai_resumed_by_command"' in source
    assert '"mirrored": False' in source
    assert 'action="whatsapp.ai_resumed_by_business_app_command"' in source
