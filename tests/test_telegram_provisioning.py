from pathlib import Path


SCRIPT = Path("scripts/provision_telegram_channel.sh").read_text(encoding="utf-8")


def test_telegram_provisioning_runs_inside_workflow_container():
    assert 'exec -T workflow-engine' in SCRIPT
    assert "XVOND_TELEGRAM_ROUTES_JSON" in SCRIPT
    assert "XVOND_CHANNEL_ROUTES_JSON" in SCRIPT
    assert "N8N_WEBHOOK_URL" in SCRIPT


def test_telegram_provisioning_uses_secret_webhook_and_verifies_provider_state():
    assert "setWebhook" in SCRIPT
    assert "secret_token" in SCRIPT
    assert "getWebhookInfo" in SCRIPT
    assert "x-telegram-bot-api-secret-token" not in SCRIPT.lower()
    assert "/webhook/xvond-telegram-inbound" in SCRIPT
    assert "/webhook/xvond-telegram-provider" in SCRIPT
    assert "provider secret mismatch" in SCRIPT


def test_telegram_provisioning_never_prints_bot_token():
    assert "console.log(JSON.stringify({" in SCRIPT
    printed = SCRIPT.split("console.log(JSON.stringify({", 1)[1]
    assert "bot_token:" not in printed
    assert "webhook_secret:" not in printed
    assert "provider_secret:" not in printed
