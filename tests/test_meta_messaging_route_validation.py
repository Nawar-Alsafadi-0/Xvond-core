from pathlib import Path


SCRIPT = Path("scripts/validate_meta_messaging_route.sh").read_text(encoding="utf-8")


def test_meta_route_validation_runs_inside_workflow_container():
    assert 'exec -T workflow-engine' in SCRIPT
    assert "XVOND_META_MESSAGING_ROUTES_JSON" in SCRIPT
    assert "XVOND_CHANNEL_ROUTES_JSON" in SCRIPT
    assert "xvond-meta-messaging-provider" in SCRIPT


def test_meta_route_validation_checks_instagram_or_messenger_identity():
    assert "channel_type must be instagram or messenger" in SCRIPT
    assert "graph.instagram.com" in SCRIPT
    assert "graph.facebook.com" in SCRIPT
    assert "?fields=id" in SCRIPT
    assert "provider identity check failed" in SCRIPT
    assert "provider secret mismatch" in SCRIPT


def test_meta_route_validation_never_prints_provider_credentials():
    assert "console.log(JSON.stringify({" in SCRIPT
    printed = SCRIPT.split("console.log(JSON.stringify({", 1)[1]
    assert "access_token:" not in printed
    assert "app_secret:" not in printed
    assert "provider_secret:" not in printed
