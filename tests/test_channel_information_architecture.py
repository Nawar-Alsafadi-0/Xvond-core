from pathlib import Path

from backend.app.modules.channels.catalog import CHANNEL_CATALOG


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_whatsapp_channel_does_not_duplicate_employee_language_or_dialect():
    ui = read("frontend/admin/simple-company.js")
    assert 'id="simple-wa-language"' not in ui
    assert 'id="simple-wa-dialect"' not in ui
    assert "Reply language and dialect come from the AI Employee profile" in ui
    assert "WhatsApp-only Instructions" in ui

    fields = {
        item["name"]: item
        for item in CHANNEL_CATALOG["whatsapp"]["config_fields"]
    }
    assert "language" not in fields
    assert "dialect" not in fields
    assert fields["graph_api_version"]["default"] == "v26.0"

    behavior = read("backend/app/modules/channels/behavior.py")
    assert "legacy_language" not in behavior
    assert "legacy_dialect" not in behavior
    assert "AI Employee profile is authoritative" in behavior


def test_whatsapp_external_identity_has_one_owner_across_employees():
    channels_api = read("backend/app/api/admin_channels.py")
    admin_signup = read("backend/app/api/admin_meta_whatsapp.py")
    customer_signup = read("backend/app/api/customer_meta_whatsapp.py")

    assert "def _assert_unique_whatsapp_phone_number_id" in channels_api
    assert "already assigned to another AI Employee" in channels_api
    assert channels_api.count("_assert_unique_whatsapp_phone_number_id(") >= 4
    assert "_assert_unique_whatsapp_phone_number_id(" in admin_signup
    assert "_assert_unique_whatsapp_phone_number_id(" in customer_signup


def test_voice_channel_separates_transport_language_from_employee_language():
    ui = read("frontend/admin/voice-admin.js")
    assert 'id="voice-dialect"' not in ui
    assert "Speech Recognition Language" in ui
    assert "does not override the employee's reply-language policy" in ui
    assert "Human takeover" in ui
    assert "Not connected" in ui


def test_customer_employee_cards_explain_shared_brain_across_channels():
    ui = read("frontend/customer/agent-channel-status.js")
    assert "Customer Channels" in ui
    assert (
        "The same employee identity, knowledge and allowed actions are used across these channels."
        in ui
    )
    assert "Live" in ui
    assert "Setup required" in ui
    assert "Xvond adapter required" in ui


def test_customer_business_profile_is_company_level_not_employee_level():
    index = read("frontend/customer/index.html")
    ia = read("frontend/customer/portal-information-architecture.js")
    assert 'id="page-business-profile"' in index
    assert (
        "The single source of truth for company facts shared with every AI employee"
        in index
    )
    assert "Company Identity" in ia
    assert "Region & Language" in ia
    assert "Working Hours" in ia
    assert "Open Business Profile" in ia
    assert "button.remove()" in ia


def test_customer_facing_service_name_is_ai_employees():
    catalog = read("backend/app/modules/solutions/catalog.py")
    assert '"name": "AI Employees"' in catalog
    assert '"name": "AI Agents"' not in catalog


def test_admin_is_not_a_customer_conversation_control_plane():
    simple = read("frontend/admin/simple-company.js")
    privacy = read("frontend/admin/privacy-boundaries.js")
    assert "takeOverConversation" not in simple
    assert "returnConversationToAI" not in simple
    assert "Customer-created content is intentionally not loaded into Admin." in privacy
    assert "requests: []" in privacy
    assert "conversations: []" in privacy
    assert "handoffs: []" in privacy

def test_customer_channel_management_has_dedicated_center_and_employee_shortcuts():
    whatsapp = read("frontend/customer/meta-whatsapp.js")
    center = read("frontend/customer/channel-center.js")
    manager = read("frontend/customer/manager-knowledge-controls.js")
    assert "openCustomerMetaWhatsAppConnect" in center
    assert 'document.getElementById("page-channels")' in center
    assert "xvondDecorateCustomerAgentsWithWhatsApp" in whatsapp
    assert 'selfService && !slots.includes("whatsapp")' in whatsapp
    assert "(config.ready && config.can_edit !== false)" in whatsapp
    assert "renderCustomerChannelsTab" in manager
    assert "xvondChannelCenterCard" in manager

def test_customer_employee_manager_includes_channels_and_systems():
    manager = read("frontend/customer/manager-knowledge-controls.js")
    assert 'managerTabButton("Overview", "overview")' in manager
    assert 'managerTabButton("Behavior", "behavior")' in manager
    assert 'managerTabButton("Knowledge", "knowledge")' in manager
    assert 'managerTabButton("Channels", "channels")' in manager
    assert 'managerTabButton("Connected Systems", "systems")' in manager
    assert "renderCustomerChannelsTab" in manager
    assert "xvondChannelCenterCard" in manager
    assert "renderCustomerSystemsTab" in manager
    assert "/customer/agents/manage/integrations" in manager



def test_customer_managed_text_channels_expose_behavior_settings_without_provider_secrets():
    from pathlib import Path

    backend = Path("backend/app/api/customer_agents.py").read_text(encoding="utf-8")
    frontend = Path("frontend/customer/channel-center.js").read_text(encoding="utf-8")

    assert '_GENERIC_CUSTOMER_BEHAVIOR_CHANNELS = {"telegram", "email", "sms", "slack", "teams", "custom"}' in backend
    assert '@router.get("/{agent_id}/channels/{channel_type}/settings")' in backend
    assert '@router.put("/{agent_id}/channels/{channel_type}/settings")' in backend
    assert "channel_instructions" in backend
    assert "provider_secret" not in backend.split("def customer_channel_behavior_settings", 1)[1].split("def update_customer_channel_behavior_settings", 1)[0]
    assert "openCustomerGenericChannelSettings" in frontend
    assert '["telegram","email","sms","slack","teams","custom"]' in frontend
