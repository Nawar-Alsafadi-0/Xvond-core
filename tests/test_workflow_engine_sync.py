from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_workflow_sync_imports_and_activates_both_xvond_gateways():
    script = read("scripts/sync_workflow_engine.sh")

    assert "xvond-actions.workflow.json" in script
    assert "xvond-channel-inbound.workflow.json" in script
    assert "publish:workflow" in script
    assert "update:workflow" in script
    assert "--active=true" in script
    assert "xvond-actions" in script
    assert "xvond-channel-inbound" in script
    assert "invalid_contract" in script


def test_actions_workflow_validates_managed_provider_urls_without_url_global():
    workflow = read("ops/n8n/xvond-actions.workflow.json")

    assert "providerUrlValid" in workflow
    assert "new URL(providerUrl)" not in workflow
    assert "invalid_channel_provisioning" in workflow


def test_workflow_engine_startup_syncs_source_controlled_workflows():
    startup = read("scripts/workflow_engine_up.sh")

    assert "sync_workflow_engine.sh" in startup
    assert "synced from Git" in startup


def test_workflow_compose_exposes_delivery_confirmation_callback():
    compose = read("docker-compose.production.yml")
    env = read(".env.example")

    assert "XVOND_INTERNAL_CHANNEL_CONFIRM_URL" in compose
    assert (
        "XVOND_INTERNAL_CHANNEL_CONFIRM_URL=http://app:8000/internal/channels/delivery-confirmed"
        in env
    )


def test_workflow_sync_verifies_running_runtime_matches_git():
    script = read("scripts/sync_workflow_engine.sh")

    assert "verify_runtime_workflow" in script
    assert "n8n export:workflow" in script
    assert "runtime_workflow_drift" in script
    assert "runtime-verified from Git" in script
