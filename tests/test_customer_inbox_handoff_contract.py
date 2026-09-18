import inspect
from pathlib import Path

from backend.app.api import customer_inbox


def test_customer_inbox_exposes_human_handoff_actions():
    source = inspect.getsource(customer_inbox)
    assert '@router.post("/{conversation_id}/take-over")' in source
    assert '@router.post("/{conversation_id}/return-ai")' in source
    assert '@router.post("/{conversation_id}/message")' in source
    assert "require_customer_operator" in source
    assert "require_customer_manager" not in source
    assert "resume_ai(session)" in source
    assert 'role="human"' in source


def test_customer_inbox_returns_current_mode_channel_and_assignment():
    source = inspect.getsource(customer_inbox._conversation_meta)
    assert '"mode": handoff["mode"]' in source
    assert '"handoff_status": handoff["handoff_status"]' in source
    assert '"handoff_assigned_user_id"' in source
    assert '"handoff_assigned_to_me"' in source
    assert '"handoff_claimable"' in source
    assert '"handoff_can_return_ai"' in source
    assert "_handoff_capabilities(channel_type)" in source
    assert "**capabilities" in source


def test_handoff_capability_matrix_matches_real_delivery_adapters():
    whatsapp = customer_inbox._handoff_capabilities("whatsapp")
    website = customer_inbox._handoff_capabilities("website")
    instagram = customer_inbox._handoff_capabilities("instagram")
    voice = customer_inbox._handoff_capabilities("voice")
    unknown = customer_inbox._handoff_capabilities("future_channel")

    assert whatsapp == {
        "handoff_supported": True,
        "human_reply_supported": True,
        "human_reply_delivery": "whatsapp",
    }
    assert website == {
        "handoff_supported": True,
        "human_reply_supported": True,
        "human_reply_delivery": "website_widget",
    }
    assert instagram == {
        "handoff_supported": True,
        "human_reply_supported": True,
        "human_reply_delivery": "n8n_channel",
    }
    assert voice["handoff_supported"] is False
    assert voice["human_reply_supported"] is False
    assert unknown["handoff_supported"] is False
    assert unknown["human_reply_supported"] is False


def test_customer_inbox_orders_by_latest_message_activity():
    source = inspect.getsource(customer_inbox.list_inbox)
    assert "func.max(AIMessage.id)" in source
    assert "latest_message_id.desc().nullslast()" in source
    assert "AIConversation.id.desc()" in source


def test_customer_portal_takeover_claims_one_human_owner():
    source = inspect.getsource(customer_inbox.take_over_conversation)
    assert "_claim_handoff(handoff, current_user)" in source
    assert '"assigned_user_id": current_user.id' in source
    assert "activate_human_handoff(" in source
    assert "human_message=True" not in source


def test_handoff_claim_rejects_duplicate_teammate_ownership():
    source = inspect.getsource(customer_inbox._claim_handoff)
    assert "handoff.assigned_user_id" in source
    assert "current_user.id" in source
    assert "already owned by another teammate" in source
    assert 'handoff.status = "in_progress"' in source
    assert "handoff.taken_over_at" in source


def test_customer_inbox_audits_handoff_lifecycle_without_message_content():
    source = inspect.getsource(customer_inbox)
    assert 'action="customer_inbox.handoff_started"' in source
    assert 'action="customer_inbox.ai_resumed"' in source
    assert 'action="customer_inbox.human_reply_sent"' in source
    audit_helper = inspect.getsource(customer_inbox._audit_handoff)
    assert 'resource_type="conversation"' in audit_helper
    assert '"external_contact_id"' in audit_helper
    assert '"content"' not in audit_helper


def test_whatsapp_human_reply_is_durable_and_idempotent_before_network_delivery():
    source = inspect.getsource(customer_inbox.send_human_reply)
    ownership_position = source.index("_require_handoff_owner")
    key_position = source.index("idempotency_key")
    message_position = source.index("message = AIMessage(")
    delivery_position = source.index("ensure_delivery(")
    commit_position = source.index("db.commit()", delivery_position)
    attempt_position = source.index("attempt_delivery(", commit_position)

    assert ownership_position < key_position < message_position < delivery_position
    assert delivery_position < commit_position < attempt_position
    assert "client_message_id" in source
    assert "existing_delivery" in source
    assert 'action="customer_inbox.human_reply_prepared"' in source
    assert "Previous WhatsApp delivery outcome is unknown; do not resend blindly" in source
    assert "WhatsApp delivery outcome is unknown; the reply is recorded for reconciliation and will not be resent automatically" in source
    assert "WhatsApp delivery was rejected temporarily; the saved reply can be retried without duplication" in source


def test_n8n_managed_channel_human_reply_uses_generic_delivery_gateway():
    source = inspect.getsource(customer_inbox.send_human_reply)
    assert 'delivery == "n8n_channel"' in source
    assert "n8n_channel_gateway.send_message" in source
    assert "external_contact_id" in source
    assert "idempotency_key" in source
    assert 'action="customer_inbox.human_reply_prepared"' in source


def test_website_human_reply_uses_canonical_conversation_delivery():
    source = inspect.getsource(customer_inbox.send_human_reply)
    assert 'delivery == "website_widget"' in source
    assert 'message = AIMessage(' in source
    assert 'role="human"' in source
    assert 'action="customer_inbox.human_reply_sent"' in source


def test_unsupported_channels_do_not_offer_fake_takeover():
    source = inspect.getsource(customer_inbox.take_over_conversation)
    assert "_require_handoff_supported(conversation)" in source
    ui = Path("frontend/customer/handoff-inbox.js").read_text(encoding="utf-8")
    assert "conversation.handoff_supported === true" in ui
    assert "conversation.human_reply_supported === true" in ui
    assert "Xvond will not show a fake reply control" in ui


def test_return_to_ai_has_owner_or_manager_guard_and_lifecycle_timestamp():
    source = inspect.getsource(customer_inbox.return_conversation_to_ai)
    guard_position = source.index("_require_handoff_close_permission")
    completed_position = source.index('handoff.status = "completed"')
    timestamp_position = source.index("handoff.completed_at")
    resume_position = source.index("resume_ai(session)")
    assert guard_position < completed_position < timestamp_position < resume_position
    guard = inspect.getsource(customer_inbox._require_handoff_close_permission)
    assert "HANDOFF_MANAGER_ROLES" in guard
    assert "assigned teammate or a manager" in guard


def test_customer_portal_loads_owned_handoff_ui():
    index = Path("frontend/customer/index.html").read_text(encoding="utf-8")
    ui = Path("frontend/customer/handoff-inbox.js").read_text(encoding="utf-8")
    assert "/static/customer/handoff-inbox.js" in index
    assert "Return to AI" in ui
    assert "Take Over" in ui
    assert "Claim Conversation" in ui
    assert "Assigned to you" in ui
    assert "Owned by teammate" in ui
    assert "prevent duplicate replies" in ui
    assert "/return-ai" in ui
    assert "/take-over" in ui
    assert "/message" in ui
    assert "client_message_id" in ui


def test_customer_inbox_live_refresh_preserves_composer_until_thread_changes():
    ui = Path("frontend/customer/handoff-inbox.js").read_text(encoding="utf-8")
    assert "startInboxLiveRefresh" in ui
    assert "2500" in ui
    assert "activeInboxFingerprint" in ui
    assert "handoff_assigned_user_id" in ui
    assert "fingerprint === activeInboxFingerprint" in ui
    assert "preserveThread: true" in ui


def test_handoff_model_and_migration_have_explicit_operator_ownership():
    model = Path("backend/app/modules/tools/business_models.py").read_text(encoding="utf-8")
    migration = Path(
        "migrations/versions/b6e1c4f8a930_add_handoff_ownership.py"
    ).read_text(encoding="utf-8")
    assert "assigned_user_id" in model
    assert "taken_over_at" in model
    assert "completed_at" in model
    assert "updated_at" in model
    assert 'down_revision = "a4d9e7c3f210"' in migration
    assert '"assigned_user_id"' in migration
    assert '"taken_over_at"' in migration
    assert '"completed_at"' in migration
