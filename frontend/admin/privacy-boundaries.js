// Xvond Admin is the operator/configuration control plane.
// Customer conversations and customer-created business request payloads belong
// to the tenant Customer Portal (or the tenant's connected external system).

(function installAdminPrivacyBoundaries() {
    const originalRenderCompanyControlCenter = renderCompanyControlCenter;
    const originalRenderOverviewTab = renderOverviewTab;
    const originalRenderAgentActionsEditor = renderAgentActionsEditor;

    function removePrivateCustomerControls() {
        document.querySelectorAll('.workspace-tab').forEach(button => {
            const label = (button.textContent || '').trim();
            if (label === 'Conversations') {
                button.remove();
            }
        });

        document.querySelectorAll('.employee-actions button').forEach(button => {
            const action = button.getAttribute('onclick') || '';
            if (action.includes('openHumanTakeover') || action.includes('openHumanConversation')) {
                button.remove();
            }
        });

        document.querySelectorAll('.metric-card').forEach(card => {
            const label = (card.querySelector('span')?.textContent || '').trim();
            if (label === 'Open Operations' || label === 'Conversations') {
                card.remove();
            }
        });
    }

    function renderAgentActionsAfterStateMutation() {
        // The legacy renderer collects values from the currently mounted cards
        // before every render. After adding/removing/replacing actions, those
        // cards still describe the previous array and would overwrite the new
        // editor state. Skip that one collection pass after an intentional
        // state mutation; all ordinary field-driven re-renders still collect.
        const originalCollect = collectCurrentActionEditor;
        collectCurrentActionEditor = function skipCollectAfterMutation() {};
        try {
            renderAgentActionsEditor();
        } finally {
            collectCurrentActionEditor = originalCollect;
        }
    }

    renderOverviewTab = function renderPrivacyAwareOverview() {
        const readiness = xvondWorkspace.data?.readiness;
        if (readiness) {
            // Backend canonical field. Keep this compatibility assignment until
            // every old admin renderer has migrated off profile_ready.
            readiness.profile_ready = readiness.company_profile_ready;
        }
        return originalRenderOverviewTab();
    };

    renderOperationsTab = function renderPrivacySafeOperations() {
        const data = xvondWorkspace.data || {};
        const items = data.unresolved || [];
        return `
            <div class="workspace-panel">
                <div class="workspace-panel-head">
                    <div>
                        <h3>External Reconciliation</h3>
                        <p>Technical operation metadata only. Customer payloads remain inside the tenant workspace.</p>
                    </div>
                    <button class="table-button" onclick="loadCompanyControlCenter(${Number(xvondWorkspace.companyId)},'operations')">Refresh</button>
                </div>
                <div class="workspace-metrics compact">
                    <div class="metric-card"><span>Unresolved</span><strong>${items.length}</strong><small>external execution outcomes</small></div>
                </div>
                ${items.length ? `<div class="operation-list">${items.map(item => `
                    <div class="request-card">
                        <div class="request-card-head">
                            <div>
                                <strong>${f((item.action_type || 'operation').replaceAll('_',' '))} #${item.id}</strong>
                                <div class="meta">${f(wsAgentName(item.agent_id))} · ${f(item.status || 'unknown')} · ${wsDate(item.created_at)}</div>
                            </div>
                            ${wsPill(item.status || 'unknown', item.status === 'external_failed' ? 'bad' : 'neutral')}
                        </div>
                        <div class="workspace-inline-actions">
                            <button class="table-button" onclick="reconcilePrivacySafeOperation(${item.id},'executed')">Executed</button>
                            <button class="table-button" onclick="reconcilePrivacySafeOperation(${item.id},'not_executed')">Not executed</button>
                            <button class="table-button" onclick="reconcilePrivacySafeOperation(${item.id},'cancelled')">Cancelled</button>
                        </div>
                    </div>
                `).join('')}</div>` : wsEmpty('No unresolved external operations','External execution state is reconciled.')}
            </div>
        `;
    };

    window.reconcilePrivacySafeOperation = async function reconcilePrivacySafeOperation(requestId, outcome) {
        const label = outcome.replaceAll('_', ' ');
        if (!confirm(`Mark external operation #${requestId} as ${label}? Use this only after verifying the external system.`)) return;
        try {
            await api(`/admin/operations/requests/${requestId}/reconcile`, {
                method: 'PATCH',
                body: JSON.stringify({outcome})
            });
            await loadCompanyControlCenter(xvondWorkspace.companyId, 'operations');
        } catch (error) {
            alert(error.message);
        }
    };

    // One company lifecycle source of truth. The old /admin/production routes
    // remain backend compatibility wrappers, but the live Admin UI uses the
    // canonical status transition directly.
    toggleWorkspaceCompany = async function toggleCanonicalWorkspaceCompany(active) {
        try {
            await api(`/admin/companies/${xvondWorkspace.companyId}/status`, {
                method: 'PATCH',
                body: JSON.stringify({active: Boolean(active)})
            });
            await loadCompanyControlCenter(xvondWorkspace.companyId, 'overview');
        } catch (error) {
            alert(error.message);
        }
    };

    renderCompanyControlCenter = function renderPrivacyAwareCompanyControlCenter() {
        if (xvondWorkspace.tab === 'conversations') {
            xvondWorkspace.tab = 'overview';
        }
        originalRenderCompanyControlCenter();
        removePrivateCustomerControls();
    };

    renderAgentActionsEditor = function renderPrivacyAwareAgentActionsEditor() {
        originalRenderAgentActionsEditor();
        document.querySelectorAll('#modal-body .modal-section-divider').forEach(section => {
            const heading = (section.querySelector('h3')?.textContent || '').trim();
            if (heading === 'Real Customer Operations') {
                section.remove();
            }
        });
    };

    // Preserve deliberate action-array mutations across the legacy renderer's
    // automatic DOM collection pass.
    addCustomAgentAction = function addPrivacyAwareCustomAgentAction() {
        collectCurrentActionEditor();
        xvondActionEditor.actions.push({
            key: `custom_operation_${xvondActionEditor.actions.length + 1}`,
            label: 'Custom Operation',
            module: '',
            description: '',
            enabled: false,
            fields: [
                {key: 'customer_name', label: 'Customer name', required: true, type: 'text'},
                {key: 'phone', label: 'Phone', required: true, type: 'text'},
            ],
            confirmation_required: true,
            availability: {mode: 'none'},
            destination: {type: 'unconfigured'},
        });
        renderAgentActionsAfterStateMutation();
    };

    removeAgentAction = function removePrivacyAwareAgentAction(index) {
        collectCurrentActionEditor();
        xvondActionEditor.actions.splice(index, 1);
        renderAgentActionsAfterStateMutation();
    };

    applySuggestedAgentActionTemplate = function applyPrivacyAwareSuggestedTemplate(id) {
        collectCurrentActionEditor();
        const template = xvondActionEditor.templates.find(item => item.id === id);
        if (!template) return;
        xvondActionEditor.templateId = template.id;
        xvondActionEditor.actions = deepClone(template.actions || []).map(action => ({
            ...action,
            enabled: false,
        }));
        renderAgentActionsAfterStateMutation();
    };

    applyAgentActionTemplate = function applyPrivacyAwareTemplate() {
        const id = document.getElementById('aa-template')?.value || '';
        const template = xvondActionEditor.templates.find(item => item.id === id);
        if (!template) return;
        applySuggestedAgentActionTemplate(template.id);
    };

    async function loadPrivacyAwareAgentMetadata(companyId, issues) {
        const agents = xvondWorkspace.data?.view?.agents || [];
        return Promise.all(agents.map(async agent => {
            const [agentProfile, knowledge, actions] = await Promise.all([
                wsOptional(`/admin/ai-employee-profile/companies/${companyId}/${agent.id}`, {name: agent.name}, `${agent.name} profile`, issues, 6000),
                wsOptional(`/admin/ai-employees/companies/${companyId}/${agent.id}/knowledge`, {items: []}, `${agent.name} knowledge`, issues, 6000),
                wsOptional(`/admin/agent-actions/${agent.id}`, {actions: [], ready: false}, `${agent.name} actions`, issues, 6000),
            ]);
            return {
                agent,
                profile: agentProfile,
                knowledge: knowledge.items || [],
                actions: actions.actions || [],
                operationsReady: !!actions.ready,
            };
        }));
    }

    hydrateWorkspaceTab = async function hydratePrivacyAwareWorkspaceTab(tab) {
        const data = xvondWorkspace.data;
        if (!data || data.loadedTabs?.has(tab)) return;
        const companyId = xvondWorkspace.companyId;
        const issues = data.loadIssues || [];

        try {
            if (['agents', 'knowledge', 'operations'].includes(tab)) {
                data.agentMeta = await loadPrivacyAwareAgentMetadata(companyId, issues);
            }
            if (tab === 'integrations') {
                const result = await wsOptional(`/admin/integrations/companies/${companyId}`, {integrations: []}, 'Integrations', issues, 6000);
                data.integrations = result.integrations || [];
            }
            if (tab === 'operations') {
                const [unresolved, catalog] = await Promise.all([
                    wsOptional(`/admin/operations/companies/${companyId}/external-unresolved`, {requests: []}, 'External reconciliation', issues, 6000),
                    wsOptional('/admin/agent-actions/templates/catalog', {templates: []}, 'Action catalog', issues, 6000),
                ]);
                data.unresolved = unresolved.requests || [];
                data.catalog = catalog;
            }
            if (tab === 'usage') {
                data.usage = await wsOptional(`/admin/operations/companies/${companyId}/usage`, {summary: {}, usage: []}, 'Usage', issues, 6000);
            }
            if (tab === 'billing') {
                const plans = await wsOptional('/admin/service-billing/plans', {plans: []}, 'Plan catalog', issues, 6000);
                data.plans = plans.plans || [];
            }
            if (tab === 'users') {
                const users = await wsOptional(`/admin/company-users/companies/${companyId}`, {users: []}, 'Company users', issues, 6000);
                data.users = (users.users && users.users.length ? users.users : data.view.users) || [];
            }
            if (tab === 'logs') {
                const audit = await wsOptional(`/admin/audit/?company_id=${companyId}&limit=100`, {logs: [], total: 0}, 'Audit trail', issues, 6000);
                data.audit = audit.logs || [];
            }
            if (tab === 'company') {
                data.setup = await wsOptional('/admin/setup/catalog', {}, 'Setup catalog', issues, 6000);
            }
            if (tab === 'workflow') {
                data.workflowEngine = await wsOptional('/admin/workflow-engine/status', {status: 'unknown', enabled: false, configured: false}, 'Workflow Engine', issues, 6000);
            }
        } finally {
            data.loadedTabs?.add(tab);
            renderCompanyControlCenter();
        }
    };

    loadCompanyControlCenter = async function loadPrivacyAwareCompanyControlCenter(companyId, tab = null) {
        simpleCompanyId = Number(companyId);
        xvondWorkspace.companyId = Number(companyId);
        if (tab && tab !== 'conversations') {
            xvondWorkspace.tab = tab;
        } else if (xvondWorkspace.tab === 'conversations') {
            xvondWorkspace.tab = 'overview';
        }

        const loadIssues = [];
        const viewController = new AbortController();
        const viewTimer = setTimeout(() => viewController.abort(), 8000);
        let view;
        try {
            view = await api(`/admin/company-view/${companyId}`, {signal: viewController.signal});
        } finally {
            clearTimeout(viewTimer);
        }

        const [channelResult, moduleResult, profile, serviceBilling, readiness] = await Promise.all([
            wsOptional(`/admin/channels/companies/${companyId}`, {channels: []}, 'Channels', loadIssues, 6000),
            wsOptional(`/admin/companies/${companyId}/modules`, {modules: []}, 'Company capabilities', loadIssues, 6000),
            wsOptional(`/admin/company-profile/${companyId}`, {company_name: view.company?.name || ''}, 'Company profile', loadIssues, 6000),
            wsOptional(`/admin/service-billing/companies/${companyId}`, {services: []}, 'Billing', loadIssues, 6000),
            wsOptional(`/admin/production/companies/${companyId}/readiness`, null, 'Managed delivery readiness', loadIssues, 6000),
        ]);

        const billingServices = serviceBilling.services || [];
        xvondWorkspace.data = {
            view,
            channels: channelResult.channels || [],
            modules: moduleResult.modules || [],
            catalog: {templates: []},
            integrations: [],
            // Customer-created content is intentionally not loaded into Admin.
            requests: [],
            conversations: [],
            handoffs: [],
            unresolved: [],
            usage: {summary: {}, usage: []},
            profile,
            setup: {},
            audit: [],
            billingServices,
            plans: [],
            users: view.users || [],
            readiness,
            agentMeta: (view.agents || []).map(agent => ({
                agent,
                profile: {name: agent.name},
                knowledge: [],
                actions: [],
                operationsReady: false,
            })),
            workflowEngine: {status: 'unknown', enabled: false, configured: false},
            loadIssues,
            loadedTabs: new Set(['overview']),
        };

        renderCompanyControlCenter();
        if (xvondWorkspace.tab !== 'overview') {
            await hydrateWorkspaceTab(xvondWorkspace.tab);
        }
    };

    openAgentActions = async function openPrivacyAwareAgentActions(companyId, agentId) {
        try {
            const [cfg, templates, integrations, profile, companyModules] = await Promise.all([
                api(`/admin/agent-actions/${agentId}`),
                api('/admin/agent-actions/templates/catalog'),
                api(`/admin/integrations/companies/${companyId}`),
                api(`/admin/ai-employee-profile/companies/${companyId}/${agentId}`),
                api(`/admin/companies/${companyId}/modules`),
            ]);
            xvondActionEditor = {
                companyId: Number(companyId),
                agentId: Number(agentId),
                templateId: cfg.template_id || null,
                actions: deepClone(cfg.actions || []),
                templates: templates.templates || [],
                businessModules: templates.business_modules || [],
                companyModules: companyModules.modules || [],
                integrations: integrations.integrations || [],
                requests: [],
                businessType: profile.business_type || '',
            };
            renderAgentActionsEditor();
        } catch (error) {
            alert(error.message);
        }
    };

    openSimpleCompany = loadCompanyControlCenter;
    window.openCompany = loadCompanyControlCenter;
})();
