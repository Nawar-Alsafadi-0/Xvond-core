function customerManagerAccess() {
    return portalOverview?.portal?.access_level === "manager";
}

function customerRoleLabel(role) {
    if (role === "employee") return "Staff";
    if (role === "manager") return "Manager";
    if (role === "owner") return "Owner";
    if (role === "admin") return "Company Admin";
    return role || "User";
}

function customerCanManageUser(user) {
    if (!user || user.id === currentUser?.id) return false;
    if (["owner", "admin"].includes(user.role)) return false;
    if (currentUser?.role === "manager") return user.role === "employee";
    return ["owner", "admin"].includes(currentUser?.role) && ["manager", "employee"].includes(user.role);
}

function customerAssignableRoleOptions() {
    const options = [["employee", "Staff — Overview only"]];
    if (["owner", "admin"].includes(currentUser?.role)) {
        options.push(["manager", "Manager — Company management access"]);
    }
    return options;
}

const customerBaseFallbackNavigation = fallbackPortalNavigation;
fallbackPortalNavigation = function() {
    if (!["owner", "admin", "manager"].includes(currentUser?.role)) {
        return [
            {id: "dashboard", label: "Overview", loader: "dashboard", group: "Workspace"}
        ];
    }
    return customerBaseFallbackNavigation();
};

const customerBaseRenderDashboard = renderDashboard;
renderDashboard = function() {
    if (customerManagerAccess()) {
        customerBaseRenderDashboard();
        return;
    }
    const summary = portalOverview?.summary || {};
    const cardTarget = document.getElementById("dashboard-cards");
    if (cardTarget) {
        cardTarget.innerHTML = `
            <div class="card"><span>Active AI Employees</span><strong>${safe(summary.active_agents || 0)}</strong></div>
            <div class="card"><span>Connected Channels</span><strong>${safe(summary.active_channels || 0)}</strong></div>
        `;
    }
    const serviceTarget = document.getElementById("dashboard-services");
    if (serviceTarget) {
        serviceTarget.innerHTML = '<p class="muted">Management details are available to authorized managers.</p>';
    }
};

loadAgents = async function() {
    const result = await api("/ai-agents/");
    agents = result.agents || [];
    const target = document.getElementById("agents-list");
    const selfServiceBuilderAvailable = Array.isArray(portalNavigation)
        && portalNavigation.some(item => item.id === "employee-builder");
    if (target) {
        target.innerHTML = agents.length
            ? agents.map(agent => `
                <div class="agent">
                    <div class="service-card-head">
                        <div>
                            <h3>${safe(agent.name)}</h3>
                            <p>${safe(agent.description || "")}</p>
                        </div>
                        <span class="status">${agent.enabled ? "Live" : "Draft / Paused"}</span>
                    </div>
                    ${selfServiceBuilderAvailable ? `<button onclick="openPage(\'employee-builder\')">Open employee</button>` : `<button onclick="openCustomerAgentSettings(${Number(agent.id)})">Manage</button>`}
                </div>
            `).join("") + '<div id="customer-agent-settings"></div>'
            : "<p>No AI employees available.</p>";
    }
    fillAgentSelects();
};

function customerSelect(id, label, options, selected) {
    return `<div class="form-group"><label>${safe(label)}</label><select id="${id}">${options.map(([value, text]) => `<option value="${value}" ${value === selected ? "selected" : ""}>${safe(text)}</option>`).join("")}</select></div>`;
}

async function openCustomerAgentSettings(agentId) {
    const target = document.getElementById("customer-agent-settings");
    if (!target) return;
    try {
        const d = await api(`/customer/agents/${agentId}`);
        const controls = d.controls || {};
        const runtimeControl = controls.can_enable_disable
            ? (d.enabled
                ? `<label style="display:flex;gap:8px;align-items:center;margin:14px 0"><input id="ca-enabled" type="checkbox" checked> Keep AI Employee live</label><p class="muted">You may pause a live employee. Re-activation is performed by Xvond after Delivery Readiness checks.</p>`
                : `<div class="panel" style="margin:14px 0"><strong>Awaiting Xvond Go-Live</strong><p class="muted" style="margin-bottom:0">This employee is Draft/Paused. Xvond activates production traffic only after Delivery Readiness passes.</p></div>`)
            : "";
        target.innerHTML = `
            <div class="panel" style="margin-top:18px">
                <div class="service-card-head"><div><h2>Manage ${safe(d.name)}</h2><p>Conversation behavior only. Business facts and integrations remain managed by Xvond.</p></div></div>
                ${customerSelect("ca-language", "Reply Language", [["auto","Automatic — match customer"],["ar","Arabic"],["en","English"],["ar_en","Arabic & English"]], d.reply_language || "auto")}
                ${customerSelect("ca-dialect", "Dialect", [["auto","Automatic — match customer"],["msa","Modern Standard Arabic"],["omani","Omani Arabic"],["gulf","Gulf Arabic"],["saudi","Saudi Arabic"],["emirati","Emirati Arabic"],["levantine","Levantine / Shami Arabic"],["egyptian","Egyptian Arabic"]], d.dialect || "auto")}
                ${customerSelect("ca-style", "Conversation Style", [["professional_friendly","Professional & Friendly"],["professional","Professional"],["warm","Warm & Conversational"],["concise","Concise"]], d.conversation_style || "professional_friendly")}
                ${customerSelect("ca-length", "Response Length", [["concise","Concise — recommended"],["balanced","Balanced"],["detailed","Detailed"]], d.response_length || "concise")}
                ${customerSelect("ca-clarification", "When a request is unclear", [["smart","Ask only when needed — recommended"],["ask_when_unclear","Ask one clarifying question"],["direct_first","Give a useful answer first"]], d.clarification_style || "smart")}
                ${customerSelect("ca-off-topic", "Personal or off-topic messages", [["business_redirect","Business focused — recommended"],["brief_friendly","Allow brief friendly small talk"]], d.off_topic_behavior || "business_redirect")}
                <div class="form-group"><label>Greeting</label><textarea id="ca-greeting" placeholder="Optional greeting">${safe(d.greeting || "")}</textarea></div>
                ${controls.can_edit_prompt ? `<div class="form-group"><label>Advanced Instructions</label><textarea id="ca-instructions">${safe(d.instructions || "")}</textarea></div>` : ""}
                ${runtimeControl}
                <div id="ca-message" class="error"></div>
                <button onclick="saveCustomerAgentSettings(${Number(agentId)}, ${controls.can_edit_prompt ? "true" : "false"}, ${controls.can_enable_disable ? "true" : "false"})">Save Changes</button>
            </div>
        `;
        target.scrollIntoView({behavior: "smooth", block: "start"});
    } catch (err) {
        target.innerHTML = `<div class="panel"><div class="error">${safe(err.message)}</div></div>`;
    }
}

async function saveCustomerAgentSettings(agentId, canEditPrompt, canEnableDisable) {
    const value = id => document.getElementById(id)?.value ?? "";
    const payload = {
        reply_language: value("ca-language"),
        dialect: value("ca-dialect"),
        conversation_style: value("ca-style"),
        response_length: value("ca-length"),
        clarification_style: value("ca-clarification"),
        off_topic_behavior: value("ca-off-topic"),
        greeting: value("ca-greeting")
    };
    if (canEditPrompt) payload.instructions = value("ca-instructions");
    const runtimeCheckbox = canEnableDisable ? document.getElementById("ca-enabled") : null;
    if (runtimeCheckbox) payload.enabled = !!runtimeCheckbox.checked;
    const message = document.getElementById("ca-message");
    if (message) message.textContent = "";
    try {
        await api(`/customer/agents/${agentId}`, {method: "PATCH", body: JSON.stringify(payload)});
        if (message) message.textContent = "Saved.";
        await loadAgents();
    } catch (err) {
        if (message) message.textContent = err.message;
    }
}

async function loadCompanyUsers() {
    const page = document.getElementById("page-users");
    const target = page?.querySelector(".dynamic-page-content");
    if (!target) return;
    try {
        const result = await api("/users/");
        const users = result.users || [];
        const roleOptions = customerAssignableRoleOptions();
        target.innerHTML = `
            <div class="panel" style="margin-bottom:20px">
                <h2>Team Access</h2>
                <p class="muted">Control who can sign in to this company workspace. Staff sees Overview only. Managers can manage company workspaces and Staff accounts, but not Owner or Company Admin accounts.</p>
                <div id="company-user-list">
                    ${users.map(user => `
                        <div class="agent">
                            <div class="service-card-head">
                                <div><strong>${safe(user.full_name || user.email)}</strong><p>${safe(user.email)} · ${safe(customerRoleLabel(user.role))}</p></div>
                                <span class="status">${user.active ? "Active" : "Inactive"}</span>
                            </div>
                            ${customerCanManageUser(user) ? `<button onclick="setCompanyUserStatus(${Number(user.id)}, ${user.active ? "false" : "true"})">${user.active ? "Disable" : "Activate"}</button>` : ""}
                        </div>
                    `).join("") || '<p>No users found.</p>'}
                </div>
            </div>
            <div class="panel">
                <h2>Add Team Member</h2>
                <p class="muted">${currentUser?.role === "manager" ? "Managers can add Staff accounts only." : "Owners and Company Admins can add Staff or Manager accounts."} Set an initial password and share it securely; the user can change it from Account & Security after signing in.</p>
                <div class="form-group"><label>Full Name</label><input id="cu-name" autocomplete="name"></div>
                <div class="form-group"><label>Email</label><input id="cu-email" type="email" autocomplete="email"></div>
                <div class="form-group"><label>Initial Password</label><input id="cu-password" type="password" autocomplete="new-password"></div>
                <div class="form-group"><label>Workspace Access</label><select id="cu-role">${roleOptions.map(([value,label]) => `<option value="${value}">${safe(label)}</option>`).join("")}</select></div>
                <div id="cu-message" class="error"></div>
                <button onclick="createCompanyUser()">Add Team Member</button>
            </div>
        `;
    } catch (err) {
        target.innerHTML = `<div class="panel"><div class="error">${safe(err.message)}</div></div>`;
    }
}

async function createCompanyUser() {
    const message = document.getElementById("cu-message");
    try {
        await api("/users/", {
            method: "POST",
            body: JSON.stringify({
                full_name: document.getElementById("cu-name").value.trim(),
                email: document.getElementById("cu-email").value.trim(),
                password: document.getElementById("cu-password").value,
                role: document.getElementById("cu-role").value
            })
        });
        await loadCompanyUsers();
    } catch (err) {
        if (message) message.textContent = err.message;
    }
}

async function setCompanyUserStatus(userId, active) {
    try {
        await api(`/users/${userId}/status`, {method: "PATCH", body: JSON.stringify({active})});
        await loadCompanyUsers();
    } catch (err) {
        alert(err.message);
    }
}

const customerBaseOpenPage = openPage;
openPage = async function(name, button) {
    await customerBaseOpenPage(name, button);
    const item = portalNavigation.find(entry => entry.id === name) || {};
    if ((item.loader || name) === "users") await loadCompanyUsers();
};