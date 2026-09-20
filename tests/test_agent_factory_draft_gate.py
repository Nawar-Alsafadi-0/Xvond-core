import inspect

from backend.app.modules.ai_agent.factory import AgentFactory


def test_custom_agent_factory_creates_draft_employee():
    source = inspect.getsource(AgentFactory.create_custom_agent)
    assert "enabled=False" in source
    assert "Delivery Readiness" in source


def test_template_agent_factory_creates_draft_employee():
    source = inspect.getsource(AgentFactory.create_from_template)
    assert "enabled=False" in source
    assert "never bypass" in source


def test_template_channels_start_disabled():
    source = inspect.getsource(AgentFactory.create_from_template)
    assert "AgentChannel(" in source
    assert "enabled=False" in source


def test_managed_templates_do_not_seed_legacy_business_tools():
    source = inspect.getsource(AgentFactory.create_from_template)
    assert '"sales": ["human_handoff"]' in source
    assert '"booking": ["human_handoff"]' in source
    assert '"website": ["human_handoff"]' in source
    assert '"whatsapp": ["human_handoff"]' in source
    assert '"voice": ["human_handoff"]' in source
    assert "canonical Operations Setup" in source
