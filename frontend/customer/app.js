let token = localStorage.getItem("xvond_customer_token");
let currentUser = null;
let portalOverview = null;
let portalNavigation = [];
let agents = [];
let chatConversationId = null;

function safe(value) {
    const element = document.createElement("div");
    element.textContent = value ?? "";
    return element.innerHTML;
}

function clearSession() {
    localStorage.removeItem("xvond_customer_token");
    token = null;
    currentUser = null;
    portalOverview = null;
    portalNavigation = [];
    agents = [];
    chatConversationId = null;
    document.getElementById("portal")?.classList.add("hidden");
    document.getElementById("login-screen")?.classList.remove("hidden");
}

async function api(path, options = {}) {
    const headers = {
        "Content-Type": "application/json",
        ...(options.headers || {})
    };
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(path, {...options, headers});
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (response.status === 401) {
        clearSession();
        throw new Error("Unauthorized");
    }
    if (!response.ok) {
        const detail = typeof data.detail === "string"
            ? data.detail
            : (data.detail?.message || "Request failed");
        throw new Error(detail);
    }
    return data;
}

async function login() {
    const email = document.getElementById("login-email").value.trim();
    const password = document.getElementById("login-password").value;
    const error = document.getElementById("login-error");
    error.textContent = "";
    try {
        const response = await fetch("/auth/login", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({email, password})
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Login failed");
        token = data.access_token || data.token;
        if (!token) throw new Error("Login token not returned");
        localStorage.setItem("xvond_customer_token", token);
        await startPortal();
    } catch (err) {
        error.textContent = err.message;
    }
}

async function logout() {
    const activeToken = token;
    try {
        if (activeToken) {
            await api("/auth/logout", {method: "POST", body: "{}"});
        }
    } catch (_) {
        // Session cleanup must still happen if the token is already invalid.
    } finally {
        clearSession();
    }
}

function activeServices() {
    return (portalOverview?.services || []).filter(item => item.status === "active");
}

function serviceByCode(code) {
    return (portalOverview?.services || []).find(item => item.service_code === code) || null;
}

function fallbackPortalNavigation() {
    const navigation = [
        {id: "dashboard", label: "Overview", loader: "dashboard", group: "Workspace"}
    ];
    const services = new Set(activeServices().map(item => item.service_code));
    if (services.has("ai_agents")) {
        navigation.push(
            {id: "agents", label: "AI Employees", loader: "agents", group: "AI Agents", service_code: "ai_agents"},
            {id: "chat", label: "Test AI Employee", loader: "chat", group: "AI Agents", service_code: "ai_agents"},
            {id: "conversations", label: "Conversations", loader: "conversations", group: "AI Agents", service_code: "ai_agents"},
            {id: "business", label: "Requests & Operations", loader: "business", group: "AI Agents", service_code: "ai_agents"},
            {id: "usage", label: "Usage", loader: "usage", group: "AI Agents", service_code: "ai_agents"}
        );
    }
    for (const service of activeServices()) {
        if (service.service_code === "ai_agents") continue;
        if (service.service_code === "integrations") {
            navigation.push({
                id: "integrations",
                label: "Connected Systems",
                loader: "integrations",
                group: service.service_name || "AI Integrations",
                service_code: service.service_code
            });
        } else {
            navigation.push({
                id: `service-${service.service_code}`,
                label: service.service_name || service.service_code,
                loader: "service",
                group: service.service_name || service.service_code,
                service_code: service.service_code
            });
        }
    }
    navigation.push({id: "billing", label: "Billing", loader: "billing", group: "Account"});
    return navigation;
}

function ensurePortalPage(item) {
    if (!item?.id || document.getElementById(`page-${item.id}`)) return;
    const container = document.getElementById("dynamic-pages");
    if (!container) return;
    const section = document.createElement("section");
    section.id = `page-${item.id}`;
    section.className = "page hidden";
    section.innerHTML = '<div class="dynamic-page-content"></div>';
    container.appendChild(section);
}

async function openInitialPortalPage() {
    const requestedPage = decodeURIComponent(
        String(window.location.hash || "").replace(/^#/, "")
    ).trim();
    const validRequestedPage = portalNavigation.some(item => item.id === requestedPage)
        ? requestedPage
        : null;
    const selfServiceDraft = (
        portalOverview?.company?.onboarding_source === "self_service"
        && Number(portalOverview?.summary?.active_agents || 0) === 0
        && portalNavigation.some(item => item.id === "employee-builder")
    );
    const initialPage = validRequestedPage || (selfServiceDraft ? "employee-builder" : null);
    if (!initialPage) return;

    const initialButton = [...document.querySelectorAll("#portal-nav .nav-item")]
        .find(item => item.dataset.page === initialPage);
    await openPage(initialPage, initialButton || null);
}


function renderPortalNavigation() {
    const nav = document.getElementById("portal-nav");
    if (!nav) return;
    nav.innerHTML = "";
    let currentGroup = null;
    for (const item of portalNavigation) {
        ensurePortalPage(item);
        if (item.group && item.group !== currentGroup) {
            currentGroup = item.group;
            const heading = document.createElement("div");
            heading.className = "nav-group";
            heading.textContent = item.group;
            nav.appendChild(heading);
        }
        const button = document.createElement("button");
        button.className = `nav-item${item.id === "dashboard" ? " active" : ""}`;
        button.textContent = item.label || item.id;
        button.dataset.page = item.id;
        button.addEventListener("click", () => openPage(item.id, button));
        nav.appendChild(button);
    }
}

function renderAccountInfo() {
    const target = document.getElementById("account-info");
    if (!target) return;
    const services = activeServices();
    target.innerHTML = `
        <p><strong>Name:</strong> ${safe(currentUser?.full_name || "-")}</p>
        <p><strong>Email:</strong> ${safe(currentUser?.email || "-")}</p>
        <p><strong>Role:</strong> ${safe(currentUser?.role || "-")}</p>
        <p><strong>Company:</strong> ${safe(portalOverview?.company?.name || "-")}</p>
        <p><strong>Active Services:</strong> ${safe(services.map(x => x.service_name || x.service_code).join(", ") || "None")}</p>
    `;
}

function renderDashboard() {
    const summary = portalOverview?.summary || {};
    const services = activeServices();
    const serviceCodes = new Set(services.map(item => item.service_code));
    const cards = [
        ["Active Services", services.length]
    ];
    if (serviceCodes.has("ai_agents")) {
        cards.push(["AI Employees", summary.agents || 0]);
        cards.push(["Conversations", summary.conversations || 0]);
        cards.push(["AI Requests", summary.requests || 0]);
    } else {
        if (serviceCodes.has("integrations")) cards.push(["Connected Systems", summary.integrations || 0]);
        cards.push(["Channels", summary.channels || 0]);
        cards.push(["Knowledge Sources", summary.knowledge_documents || 0]);
    }
    const cardTarget = document.getElementById("dashboard-cards");
    if (cardTarget) {
        cardTarget.innerHTML = cards.slice(0, 4).map(([label, value]) => `
            <div class="card"><span>${safe(label)}</span><strong>${safe(value)}</strong></div>
        `).join("");
    }

    const serviceTarget = document.getElementById("dashboard-services");
    if (serviceTarget) {
        serviceTarget.innerHTML = services.length
            ? services.map(service => serviceOverviewCard(service)).join("")
            : '<p class="muted">No active Xvond services.</p>';
    }
}

function formatMoney(value, currency) {
    const number = Number(value || 0);
    const amount = Number.isFinite(number) ? number.toFixed(number % 1 ? 2 : 0) : String(value || 0);
    return `${safe(currency || "OMR")} ${safe(amount)}`;
}

function formatDate(value) {
    if (!value) return "-";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? safe(value) : safe(date.toLocaleString());
}

function formatLimit(limit) {
    if (limit === 0 || limit === "0" || limit === null || limit === undefined) return "Unlimited";
    return safe(limit);
}

function metricLabel(metric) {
    return String(metric || "")
        .replaceAll("_", " ")
        .replace(/\b\w/g, letter => letter.toUpperCase());
}

function serviceUsageMarkup(service) {
    const usage = service?.usage || {};
    const entries = Object.entries(usage);
    if (!entries.length) return '<p class="muted">No metered limits on this package.</p>';
    return `<div class="usage-list">${entries.map(([metric, data]) => `
        <div class="billing-row">
            <span>${safe(metricLabel(metric))}</span>
            <strong>${safe(data?.used || 0)} / ${formatLimit(data?.limit)}</strong>
        </div>
    `).join("")}</div>`;
}

function serviceOverviewCard(service) {
    return `
        <div class="service-card">
            <div class="service-card-head">
                <div>
                    <h3>${safe(service.service_name || service.service_code)}</h3>
                    <p>${safe(service.plan?.name || "-")} · ${safe(service.plan?.tier || "-")}</p>
                </div>
                <span class="pill">${safe(service.status)}</span>
            </div>
            <strong>${formatMoney(service.plan?.monthly_price, service.plan?.currency)}</strong>
        </div>
    `;
}

function serviceDetailMarkup(service) {
    if (!service) return '<div class="panel"><p>Service is not assigned to this company.</p></div>';
    return `
        <div class="panel" style="margin-bottom:20px">
            <div class="service-card-head">
                <div>
                    <h2>${safe(service.service_name || service.service_code)}</h2>
                    <p>${safe(service.plan?.name || "-")} · ${safe(service.plan?.tier || "-")}</p>
                </div>
                <span class="pill">${safe(service.status)}</span>
            </div>
            <div class="billing-row"><span>Monthly price</span><strong>${formatMoney(service.plan?.monthly_price, service.plan?.currency)}</strong></div>
            <div class="billing-row"><span>Current period</span><strong>${formatDate(service.current_period_start)} → ${formatDate(service.current_period_end)}</strong></div>
        </div>
        <div class="panel">
            <h2>Package Usage</h2>
            ${serviceUsageMarkup(service)}
        </div>
    `;
}

function renderServicePage(serviceCode, pageId) {
    const page = document.getElementById(`page-${pageId}`);
    const target = page?.querySelector(".dynamic-page-content");
    if (!target) return;
    target.innerHTML = serviceDetailMarkup(serviceByCode(serviceCode));
}

function renderIntegrations() {
    const target = document.getElementById("customer-integrations-content");
    if (!target) return;
    const service = serviceByCode("integrations");
    const integrations = portalOverview?.integrations || [];
    target.innerHTML = `
        ${serviceDetailMarkup(service)}
        <div class="panel" style="margin-top:20px">
            <h2>Connected Systems</h2>
            ${integrations.length ? integrations.map(item => `
                <div class="agent">
                    <div class="service-card-head">
                        <div><strong>${safe(item.name)}</strong><p>${safe(item.type)}</p></div>
                        <span class="pill">${item.enabled ? "Active" : "Inactive"}</span>
                    </div>
                </div>
            `).join("") : '<p class="muted">No external systems connected yet.</p>'}
        </div>
    `;
}

function renderPaymentMethod() {
    const billing = portalOverview?.billing || {};
    const method = billing.payment_method;
    if (!billing.online_payments_enabled || !method) {
        return `
            <div class="billing-row">
                <span>Online payment method</span>
                <strong>Not configured</strong>
            </div>
        `;
    }
    const brand = method.brand || "Card";
    const last4 = method.last4 ? `•••• ${method.last4}` : "";
    return `
        <div class="billing-row">
            <span>Payment method</span>
            <strong>${safe(brand)} ${safe(last4)}</strong>
        </div>
    `;
}

function renderBilling() {
    const target = document.getElementById("customer-billing-content");
    if (!target) return;
    const services = portalOverview?.services || [];
    target.innerHTML = `
        <div class="panel" style="margin-bottom:20px">
            <h2>Billing</h2>
            ${renderPaymentMethod()}
        </div>
        <div class="service-grid">
            ${services.length ? services.map(service => `
                <div class="service-card">
                    <div class="service-card-head">
                        <div>
                            <h3>${safe(service.service_name || service.service_code)}</h3>
                            <p>${safe(service.plan?.name || "-")} · ${safe(service.plan?.tier || "-")}</p>
                        </div>
                        <span class="pill">${safe(service.status)}</span>
                    </div>
                    <div class="billing-row"><span>Monthly price</span><strong>${formatMoney(service.plan?.monthly_price, service.plan?.currency)}</strong></div>
                    <div class="billing-row"><span>Period start</span><strong>${formatDate(service.current_period_start)}</strong></div>
                    <div class="billing-row"><span>Period end</span><strong>${formatDate(service.current_period_end)}</strong></div>
                    ${serviceUsageMarkup(service)}
                </div>
            `).join("") : '<div class="panel"><p>No services assigned.</p></div>'}
        </div>
    `;
}

async function startPortal() {
    try {
        [currentUser, portalOverview] = await Promise.all([
            api("/users/me"),
            api("/customer/overview")
        ]);
        if (!currentUser.company_id) {
            throw new Error("This account is not attached to a customer company.");
        }
        portalNavigation = portalOverview?.portal?.navigation || fallbackPortalNavigation();
        document.getElementById("login-screen").classList.add("hidden");
        document.getElementById("portal").classList.remove("hidden");
        document.getElementById("user-email").textContent = currentUser.email;
        renderPortalNavigation();
        renderAccountInfo();
        renderDashboard();
        await openInitialPortalPage();
    } catch (err) {
        clearSession();
        const error = document.getElementById("login-error");
        if (error) error.textContent = err.message;
    }
}

async function loadAgents() {
    const result = await api("/ai-agents/");
    agents = result.agents || [];
    const target = document.getElementById("agents-list");
    if (target) {
        target.innerHTML = agents.length
            ? agents.map(agent => `
                <div class="agent">
                    <h3>${safe(agent.name)}</h3>
                    <p>${safe(agent.description || "")}</p>
                    <p><span class="status">${agent.enabled ? "Active" : "Inactive"}</span></p>
                </div>
            `).join("")
            : "<p>No AI employees available.</p>";
    }
    fillAgentSelects();
}

function fillAgentSelects() {
    const html = agents.map(agent => `
        <option value="${agent.id}">${safe(agent.name)}</option>
    `).join("");
    const chatSelect = document.getElementById("chat-agent");
    const conversationSelect = document.getElementById("conversation-agent");
    if (chatSelect) chatSelect.innerHTML = html;
    if (conversationSelect) conversationSelect.innerHTML = html;
}

async function loadUsage() {
    const usage = await api("/usage/");
    const values = {
        "usage-requests": usage.requests || 0,
        "usage-input": usage.input_tokens || 0,
        "usage-output": usage.output_tokens || 0,
        "usage-total": usage.total_tokens || 0
    };
    for (const [id, value] of Object.entries(values)) {
        const element = document.getElementById(id);
        if (element) element.textContent = value;
    }
}

async function sendChat() {
    const agentId = document.getElementById("chat-agent").value;
    const input = document.getElementById("chat-message");
    const message = input.value.trim();
    if (!agentId || !message) return;
    addChat("You", message);
    input.value = "";
    try {
        const result = await api(`/ai-agents/${agentId}/chat`, {
            method: "POST",
            body: JSON.stringify({message, conversation_id: chatConversationId})
        });
        chatConversationId = result.conversation_id;
        addChat("AI Employee", result.response.content);
    } catch (err) {
        addChat("Error", err.message);
    }
}

function addChat(role, message) {
    const box = document.getElementById("chat-box");
    box.innerHTML += `<div class="chat-row"><strong>${safe(role)}</strong><div>${safe(message)}</div></div>`;
    box.scrollTop = box.scrollHeight;
}

async function loadConversations() {
    const agentId = document.getElementById("conversation-agent").value;
    if (!agentId) {
        document.getElementById("conversation-list").innerHTML = "<p>No AI employees available.</p>";
        return;
    }
    const result = await api(`/ai-agents/${agentId}/conversations`);
    const items = result.conversations || [];
    document.getElementById("conversation-list").innerHTML = items.length
        ? items.map(item => `
            <div class="conversation-item" onclick="loadConversation(${agentId},${item.id})">
                <strong>${safe(item.title || `Conversation ${item.id}`)}</strong>
            </div>
        `).join("")
        : "<p>No conversations yet.</p>";
}

async function loadConversation(agentId, conversationId) {
    const result = await api(`/ai-agents/${agentId}/conversations/${conversationId}`);
    const messages = result.messages || [];
    document.getElementById("conversation-messages").innerHTML = messages.map(message => `
        <div class="chat-row"><strong>${safe(message.role)}</strong><div>${safe(message.content)}</div></div>
    `).join("");
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
    if (loader === "chat") await loadAgents();
    if (loader === "conversations") {
        await loadAgents();
        await loadConversations();
    }
    if (loader === "usage") await loadUsage();
    if (loader === "business") await loadCustomerBusiness();
    if (loader === "integrations") renderIntegrations();
    if (loader === "billing") renderBilling();
    if (loader === "service") renderServicePage(item.service_code, item.id);
}

async function loadCustomerBusiness() {
    const target = document.getElementById("customer-business-content");
    try {
        const [operationResult, handoffs] = await Promise.all([
            api("/customer/action-requests"),
            api("/customer/business/handoffs")
        ]);
        const operations = operationResult.requests || [];
        const open = operations.filter(x => !["completed", "cancelled"].includes(x.status));
        const completed = operations.filter(x => x.status === "completed");
        document.getElementById("customer-operations-count").textContent = operations.length;
        document.getElementById("customer-open-count").textContent = open.length;
        document.getElementById("customer-completed-count").textContent = completed.length;
        document.getElementById("customer-handoffs-count").textContent = handoffs.length;
        target.innerHTML = `
            ${customerBusinessSection("Operations", operations, item => `
                <strong>${safe((item.action_type || "operation").replaceAll("_", " "))} #${item.id}</strong>
                <p>Status: ${safe(item.status)}</p>
                <p>${safe(item.summary || "")}</p>
                ${operationDetails(item.details)}
                ${operationButtons(item)}
            `)}
            ${customerBusinessSection("Human Handoffs", handoffs, item => `
                <strong>Handoff #${item.id}</strong>
                <p>Reason: ${safe(item.reason || "-")}</p>
                <p>Department: ${safe(item.department || "-")}</p>
                <p>Priority: ${safe(item.priority || "-")}</p>
                <p>Status: ${safe(item.status)}</p>
            `)}
        `;
    } catch (err) {
        target.innerHTML = `<div class="panel">${safe(err.message)}</div>`;
    }
}

function operationDetails(details) {
    const entries = Object.entries(details || {}).filter(([key, value]) =>
        !key.startsWith("_") && value !== null && value !== ""
    );
    if (!entries.length) return "";
    return `<div>${entries.map(([key, value]) => `
        <p><strong>${safe(key.replaceAll("_", " "))}:</strong> ${safe(typeof value === "object" ? JSON.stringify(value) : value)}</p>
    `).join("")}</div>`;
}

function operationButtons(item) {
    if (item.status === "awaiting_confirmation") {
        return `<p><em>Waiting for customer confirmation in the conversation.</em></p>`;
    }
    const buttons = [];
    if (!["in_progress", "processing", "completed", "cancelled"].includes(item.status)) {
        buttons.push(`<button onclick="setCustomerOperationStatus(${item.id},'in_progress')">Start</button>`);
    }
    if (!["completed", "cancelled"].includes(item.status)) {
        buttons.push(`<button onclick="setCustomerOperationStatus(${item.id},'completed')">Complete</button>`);
        buttons.push(`<button onclick="setCustomerOperationStatus(${item.id},'cancelled')">Cancel</button>`);
    }
    return buttons.length ? `<div class="chat-input">${buttons.join("")}</div>` : "";
}

async function setCustomerOperationStatus(id, status) {
    try {
        await api(`/customer/action-requests/${id}`, {
            method: "PATCH",
            body: JSON.stringify({status})
        });
        await loadCustomerBusiness();
    } catch (err) {
        alert(err.message);
    }
}

function customerBusinessSection(title, items, renderer) {
    return `
        <div class="panel" style="margin-bottom:20px">
            <h2>${safe(title)}</h2>
            ${items.length
                ? items.map(item => `<div class="agent">${renderer(item)}</div>`).join("")
                : `<p>No ${safe(title.toLowerCase())} yet.</p>`}
        </div>
    `;
}

function hidePasswordScreens() {
    ["normal-login-form", "forgot-password-form", "reset-password-form"].forEach(id => {
        document.getElementById(id)?.classList.add("hidden");
    });
}

function showNormalLogin() {
    hidePasswordScreens();
    document.getElementById("normal-login-form")?.classList.remove("hidden");
}

function showForgotPassword() {
    hidePasswordScreens();
    document.getElementById("forgot-password-form")?.classList.remove("hidden");
    const loginEmail = document.getElementById("login-email");
    const forgotEmail = document.getElementById("forgot-email");
    if (loginEmail && forgotEmail && loginEmail.value.trim()) {
        forgotEmail.value = loginEmail.value.trim();
    }
}

function showResetPassword(email) {
    hidePasswordScreens();
    document.getElementById("reset-password-form").classList.remove("hidden");
    document.getElementById("reset-email").value = email;
}

async function publicAuthRequest(path, body) {
    const response = await fetch(path, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
    });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(data.detail || "Request failed");
    return data;
}

async function requestPasswordCode() {
    const email = document.getElementById("forgot-email").value.trim();
    const message = document.getElementById("forgot-message");
    message.textContent = "";
    if (!email) {
        message.textContent = "Email is required.";
        return;
    }
    try {
        await publicAuthRequest("/auth/customer/forgot-password", {email});
        showResetPassword(email);
        document.getElementById("reset-message").textContent =
            "If this email belongs to a customer account, a verification code was sent.";
    } catch (error) {
        message.textContent = error.message;
    }
}

async function resetCustomerPassword() {
    const email = document.getElementById("reset-email").value.trim();
    const code = document.getElementById("reset-code").value.trim();
    const password = document.getElementById("reset-new-password").value;
    const confirmPassword = document.getElementById("reset-confirm-password").value;
    const message = document.getElementById("reset-message");
    message.textContent = "";
    if (!email || !code || !password) {
        message.textContent = "Complete all fields.";
        return;
    }
    if (password !== confirmPassword) {
        message.textContent = "Passwords do not match.";
        return;
    }
    try {
        await publicAuthRequest("/auth/customer/reset-password", {
            email,
            code,
            new_password: password
        });
        showNormalLogin();
        document.getElementById("login-email").value = email;
        document.getElementById("login-password").value = "";
        document.getElementById("login-error").textContent = "Password changed. You can log in now.";
    } catch (error) {
        message.textContent = error.message;
    }
}

async function changeCustomerPassword() {
    const currentPassword = document.getElementById("current-password").value;
    const newPassword = document.getElementById("new-password").value;
    const confirmPassword = document.getElementById("confirm-new-password").value;
    const message = document.getElementById("change-password-message");
    message.textContent = "";
    if (!currentPassword || !newPassword || !confirmPassword) {
        message.textContent = "Complete all password fields.";
        return;
    }
    if (newPassword !== confirmPassword) {
        message.textContent = "New passwords do not match.";
        return;
    }
    try {
        await api("/auth/customer/change-password", {
            method: "POST",
            body: JSON.stringify({current_password: currentPassword, new_password: newPassword})
        });
        document.getElementById("current-password").value = "";
        document.getElementById("new-password").value = "";
        document.getElementById("confirm-new-password").value = "";
        message.textContent = "Password changed successfully. Please sign in again.";
        setTimeout(() => clearSession(), 800);
    } catch (error) {
        message.textContent = error.message;
    }
}

if (token) startPortal();
