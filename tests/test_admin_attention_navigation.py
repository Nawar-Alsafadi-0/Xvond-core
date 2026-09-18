from pathlib import Path


APP = Path("frontend/admin/app.js").read_text(encoding="utf-8")


def test_admin_attention_navigation_uses_bound_events_not_dynamic_inline_javascript():
    assert "XVOND_ADMIN_WORKSPACE_TABS" in APP
    assert "function adminWorkspaceTab" in APP
    assert 'class="table-button admin-attention-open"' in APP
    assert 'data-workspace-tab="' in APP
    assert 'target.querySelectorAll(".admin-attention-open")' in APP
    assert 'button.addEventListener("click"' in APP
    assert "loadCompanyControlCenter(companyId,adminWorkspaceTab(button.dataset.workspaceTab))" in APP
    assert "loadCompanyControlCenter(${Number(item.company_id)},'${escapeAdmin(item.tab" not in APP
