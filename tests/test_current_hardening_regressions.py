from pathlib import Path

from backend.app.modules.channels.behavior import build_text_channel_behavior_prompt


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_whatsapp_behavior_is_channel_specific():
    prompt = build_text_channel_behavior_prompt(
        "whatsapp",
        {
            "language": "ar",
            "dialect": "omani",
            "tone": "warm",
            "response_style": "conversational",
            "response_length": "concise",
            "emoji_style": "minimal",
            "channel_instructions": "Never over-explain.",
        },
    )
    assert "WHATSAPP CHANNEL BEHAVIOR" in prompt
    assert "AI Employee profile is authoritative for reply language, dialect" in prompt
    assert "Language: ar" not in prompt
    assert "Dialect: omani" not in prompt
    assert "Tone override: warm" in prompt
    assert "Emoji style: minimal" in prompt
    assert "Never over-explain" in prompt


def test_shared_runtime_resolves_whatsapp_behavior_from_channel():
    runtime = source("backend/app/core/agent_runtime.py")
    assert "build_runtime_system_prompt" in runtime
    assert 'conversation.channel_type or ""' in runtime
    assert "get_channel_capability" in runtime
    assert "N8N_CHANNEL_ADAPTER" in runtime
    assert "build_text_channel_behavior_prompt" in runtime
    assert "system_prompt=system_prompt" in runtime


def test_existing_conversation_refreshes_external_channel_binding():
    runtime = source("backend/app/core/agent_runtime.py")
    assert ".populate_existing()" in runtime


def test_analytics_source_secrets_are_protected_and_not_returned_raw():
    models = source("backend/app/modules/analytics/models.py")
    api = source("backend/app/api/admin_analytics_builder.py")
    assert '@validates("config")' in models
    assert "protect_config" in models
    assert '"config": public_config(x.config)' in api
    assert "configured_secret_fields(x.config)" in api
    assert '"config": x.config' not in api


def test_failed_automation_runs_remain_billable_for_capacity():
    runtime = source("backend/app/modules/automation/runtime.py")
    marker = 'metadata={"workflow_id": workflow.id, "status": "failed"}'
    assert marker in runtime
    assert runtime.count("service_limits.record(") >= 2
    assert "except Exception as original_error:" in runtime
    assert 'original_error = __import__("sys").exc_info()[1]' not in runtime
    assert 'raise original_error' not in runtime
    assert '"usage_recorded": usage_recorded' in runtime


def test_voice_provisioning_is_resumable_and_readiness_gated():
    voice = source("backend/app/api/admin_voice.py")
    assert "_save_provisioning_state" in voice
    assert '"provisioning_state": "provisioning"' in voice
    assert '"provisioning_state": "connected"' in voice
    assert '"provisioning_state": "failed"' in voice
    assert "_activation_blockers" in voice
    assert "channel.enabled = not blockers" in voice


def test_pii_redaction_is_on_in_environment_template():
    env = source(".env.example")
    settings = source("backend/app/core/config/settings.py")
    engine = source("backend/app/core/ai/engine.py")
    assert "AI_PII_REDACTION_ENABLED=true" in env
    assert "AI_PII_REDACTION_ENABLED" in settings
    assert "protect_text" in engine
    assert "restore_ai_response" in engine
