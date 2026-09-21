let activeBusinessItem = null;

function ensureInboxMarkup() {
    const page = document.getElementById("page-conversations");
    if (!page || page.dataset.inboxReady === "1") return;
    page.dataset.inboxReady = "1";
    page.innerHTML = `
        <div class="panel">
            <div class="inbox-heading">
                <div>
                    <h2>Inbox</h2>
                    <p class="muted">All customer conversations across your AI employees and channels.</p>
                </div>
            </div>
            <div class="inbox-filters">
                <div>
                    <label>AI Employee</label>
                    <select id="conversation-agent" onchange="loadConversations()">
                        <option value="">All AI Employees</option>
                    </select>
                </div>
                <div>
                    <label>Channel</label>
                    <select id="conversation-channel" onchange="loadConversations()">
                        <option value="">All Channels</option>
                    </select>
                </div>
                <div class="inbox-search-wrap">
                    <label>Search</label>
                    <input id="conversation-search" placeholder="Customer, contact or conversation..." onkeydown="if(event.key==='Enter') loadConversations()">
                </div>
                <button class="inbox-search-button" onclick="loadConversations()">Search</button>
            </div>
            <div class="inbox-layout">
                <div id="conversation-list" class="inbox-list"></div>
                <div id="conversation-messages" class="conversation-messages inbox-thread">
                    <div class="empty-state">Choose a conversation to view its messages.</div>
                </div>
            </div>
        </div>
    `;
}

function inboxBadge(value, className = "") {
    return `<span class="inbox-badge ${safe(className)}">${safe(value || "-")}</span>`;
}

function populateInboxFilters(filters) {
    const agentSelect = document.getElementById("conversation-agent");
    const channelSelect = document.getElementById("conversation-channel");
    if (!agentSelect || !channelSelect) return;

    const currentAgent = agentSelect.value;
    const currentChannel = channelSelect.value;
    agentSelect.innerHTML = '<option value="">All AI Employees</option>' +
        (filters?.agents || []).map(item =>
            `<option value="${item.id}">${safe(item.name)}</option>`
        ).join("");
    channelSelect.innerHTML = '<option value="">All Channels</option>' +
        (filters?.channels || []).map(item =>
            `<option value="${safe(item.type)}">${safe(item.label)}</option>`
        ).join("");
    agentSelect.value = [...agentSelect.options].some(x => x.value === currentAgent) ? currentAgent : "";
    channelSelect.value = [...channelSelect.options].some(x => x.value === currentChannel) ? currentChannel : "";
}

async function loadConversations() {
    ensureInboxMarkup();
    const list = document.getElementById("conversation-list");
    if (!list) return;

    const agentId = document.getElementById("conversation-agent")?.value || "";
    const channelType = document.getElementById("conversation-channel")?.value || "";
    const search = document.getElementById("conversation-search")?.value.trim() || "";
    const params = new URLSearchParams();
    if (agentId) params.set("agent_id", agentId);
    if (channelType) params.set("channel_type", channelType);
    if (search) params.set("search", search);

    list.innerHTML = '<div class="empty-state">Loading conversations...</div>';
    try {
        const result = await api(`/customer/inbox${params.toString() ? `?${params}` : ""}`);
        populateInboxFilters(result.filters || {});
        const items = result.conversations || [];
        list.innerHTML = items.length ? items.map(item => {
            const preview = item.last_message?.content || item.title || "No messages";
            const contact = item.external_contact_id
                ? `<div class="inbox-contact">${safe(item.external_contact_id)}</div>`
                : "";
            return `
                <button class="inbox-item" onclick="loadInboxConversation(${item.id}, this)">
                    <div class="inbox-item-top">
                        <strong>${safe(item.title || `Conversation ${item.id}`)}</strong>
                        <span>${formatDate(item.last_message?.created_at || item.created_at)}</span>
                    </div>
                    <div class="inbox-badges">
                        ${inboxBadge(item.agent_name, "agent-badge")}
                        ${inboxBadge(item.channel_label, `channel-${item.channel_type || "unknown"}`)}
                    </div>
                    ${contact}
                    <p>${safe(preview)}</p>
                </button>
            `;
        }).join("") : '<div class="empty-state">No conversations match these filters.</div>';
    } catch (error) {
        list.innerHTML = `<div class="empty-state">${safe(error.message)}</div>`;
    }
}

async function loadInboxConversation(conversationId, button = null) {
    document.querySelectorAll(".inbox-item").forEach(item => item.classList.remove("selected"));
    if (button) button.classList.add("selected");
    const target = document.getElementById("conversation-messages");
    if (!target) return;
    target.innerHTML = '<div class="empty-state">Loading messages...</div>';
    try {
        const result = await api(`/customer/inbox/${conversationId}`);
        const conversation = result.conversation || {};
        const messages = result.messages || [];
        target.innerHTML = `
            <div class="inbox-thread-head">
                <div>
                    <h3>${safe(conversation.title || `Conversation ${conversationId}`)}</h3>
                    <div class="inbox-badges">
                        ${inboxBadge(conversation.agent_name, "agent-badge")}
                        ${inboxBadge(conversation.channel_label, `channel-${conversation.channel_type || "unknown"}`)}
                    </div>
                </div>
                ${conversation.external_contact_id ? `<strong>${safe(conversation.external_contact_id)}</strong>` : ""}
            </div>
            <div class="inbox-message-list">
                ${messages.length ? messages.map(message => `
                    <div class="inbox-message ${safe(message.role)}">
                        <strong>${safe(message.role === "assistant" ? "AI Employee" : message.role)}</strong>
                        <div>${safe(message.content)}</div>
                        <small>${formatDate(message.created_at)}</small>
                    </div>
                `).join("") : '<div class="empty-state">No messages in this conversation.</div>'}
            </div>
        `;
    } catch (error) {
        target.innerHTML = `<div class="empty-state">${safe(error.message)}</div>`;
    }
}

async function loadConversation(_agentId, conversationId) {
    return loadInboxConversation(conversationId);
}

function capabilityEmptyLabel(moduleName) {
    const labels = {
        quotation: "quotation requests",
        booking: "bookings",
        orders: "orders or requests",
        lead_management: "leads",
        customer_support: "support requests"
    };
    return labels[moduleName] || "operations";
}

function renderCapabilityOperations(target, item, operations, handoffs = []) {
    const open = operations.filter(x => !["completed", "cancelled"].includes(x.status));
    const completed = operations.filter(x => x.status === "completed");
    target.innerHTML = `
        <div class="cards capability-cards">
            <div class="card"><span>Total</span><strong>${operations.length}</strong></div>
            <div class="card"><span>Open</span><strong>${open.length}</strong></div>
            <div class="card"><span>Completed</span><strong>${completed.length}</strong></div>
            <div class="card"><span>AI Employees</span><strong>${new Set(operations.map(x => x.agent_id)).size}</strong></div>
        </div>
        ${customerBusinessSection(item.label || "Operations", operations, operation => `
            <div class="service-card-head">
                <div>
                    <strong>${safe(operation.action_label || operation.action_type)} #${operation.id}</strong>
                    <p>${safe(operation.summary || "")}</p>
                </div>
                <span class="pill">${safe(operation.status)}</span>
            </div>
            ${operationDetails(operation.details)}
            ${operationButtons(operation)}
        `)}
        ${item.include_handoffs ? customerBusinessSection("Human Handoffs", handoffs, handoff => `
            <strong>Handoff #${handoff.id}</strong>
            <p>Reason: ${safe(handoff.reason || "-")}</p>
            <p>Department: ${safe(handoff.department || "-")}</p>
            <p>Priority: ${safe(handoff.priority || "-")}</p>
            <p>Status: ${safe(handoff.status)}</p>
        `) : ""}
    `;
}

async function loadCapabilityBusiness(item) {
    activeBusinessItem = item;
    const page = document.getElementById(`page-${item.id}`);
    const target = page?.querySelector(".dynamic-page-content") || document.getElementById("customer-business-content");
    if (!target) return;
    target.innerHTML = '<div class="panel"><div class="empty-state">Loading...</div></div>';
    try {
        const moduleName = item.capability_module || "";
        const requestsPromise = api(`/customer/action-requests${moduleName ? `?module=${encodeURIComponent(moduleName)}` : ""}`);
        const [operationResult, handoffs] = await Promise.all([
            requestsPromise,
            item.include_handoffs ? api("/customer/business/handoffs") : Promise.resolve([])
        ]);
        const operations = operationResult.requests || [];
        renderCapabilityOperations(target, item, operations, handoffs || []);
        if (!operations.length && !handoffs.length) {
            const empty = target.querySelector(".panel .muted");
            if (empty) empty.textContent = `No ${capabilityEmptyLabel(moduleName)} yet.`;
        }
    } catch (error) {
        target.innerHTML = `<div class="panel">${safe(error.message)}</div>`;
    }
}

async function loadCustomerBusiness() {
    const item = activeBusinessItem || portalNavigation.find(entry => entry.loader === "business") || {
        id: "business",
        label: "Requests & Operations"
    };
    return loadCapabilityBusiness(item);
}

async function setCustomerOperationStatus(id, status) {
    try {
        await api(`/customer/action-requests/${id}`, {
            method: "PATCH",
            body: JSON.stringify({status})
        });
        if (activeBusinessItem) await loadCapabilityBusiness(activeBusinessItem);
    } catch (error) {
        alert(error.message);
    }
}

async function openPage(name, button) {
    document.querySelectorAll(".page").forEach(page => page.classList.add("hidden"));
    document.querySelectorAll(".nav-item").forEach(item => item.classList.remove("active"));
    const target = document.getElementById(`page-${name}`);
    if (!target) return;
    target.classList.remove("hidden");
    if (button) button.classList.add("active");
    const item = portalNavigation.find(entry => entry.id === name) || {};
    document.getElementById("page-title").textContent = item.label || "Xvond";

    const loader = item.loader || name;
    if (loader === "dashboard") renderDashboard();
    if (loader === "agents") await loadAgents();
    if (loader === "channels" && typeof renderXvondChannelCenter === "function") await renderXvondChannelCenter();
    if (loader === "chat") await loadAgents();
    if (loader === "conversations") {
        ensureInboxMarkup();
        await loadConversations();
    }
    if (loader === "usage") await loadUsage();
    if (loader === "business") await loadCapabilityBusiness(item);
    if (loader === "integrations") renderIntegrations();
    if (loader === "billing") renderBilling();
    if (loader === "service") renderServicePage(item.service_code, item.id);
}
