let token = localStorage.getItem("xvond_customer_token");
let currentUser = null;
let portalOverview = null;
let portalNavigation = [];
let agents = [];
let chatConversationId = null;

function setCustomerNavigationOpen(open) {
    const expanded = Boolean(open && window.matchMedia("(max-width: 720px)").matches);
    const toggle = document.getElementById("customer-menu-toggle");
    const scrim = document.getElementById("customer-nav-scrim");
    document.body.classList.toggle("customer-nav-open", expanded);
    toggle?.setAttribute("aria-expanded", String(expanded));
    toggle?.setAttribute("aria-label", expanded ? "Close navigation" : "Open navigation");
    if (toggle) toggle.querySelector("span").textContent = expanded ? "×" : "☰";
    if (scrim) scrim.tabIndex = expanded ? 0 : -1;
}

function initializeCustomerShell() {
    const toggle = document.getElementById("customer-menu-toggle");
    const scrim = document.getElementById("customer-nav-scrim");
    const sidebar = document.getElementById("customer-sidebar");
    const desktopQuery = window.matchMedia("(min-width: 721px)");
    toggle?.addEventListener("click", () => {
        setCustomerNavigationOpen(!document.body.classList.contains("customer-nav-open"));
    });
    scrim?.addEventListener("click", () => setCustomerNavigationOpen(false));
    sidebar?.addEventListener("click", event => {
        if (event.target.closest("button, a")) setCustomerNavigationOpen(false);
    });
    document.addEventListener("keydown", event => {
        if (event.key === "Escape" && document.body.classList.contains("customer-nav-open")) {
            setCustomerNavigationOpen(false);
            toggle?.focus();
        }
    });
    desktopQuery.addEventListener?.("change", event => {
        if (event.matches) setCustomerNavigationOpen(false);
    });
}

function safe(value) {
    const element = document.createElement("div");
    element.textContent = value ?? "";
    return element.innerHTML;
}

function clearSession() {
    setCustomerNavigationOpen(false);
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
    const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
    const headers = {
        ...(isFormData ? {} : {"Content-Type": "application/json"}),
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

async function renderIntegrations() {
    const target = document.getElementById("customer-integrations-content")
        || document.querySelector("#page-integrations .dynamic-page-content");
    if (!target) return;
    target.innerHTML = '<div class="panel"><p class="muted">Loading connected systems…</p></div>';

    try {
        const [catalogResult, listResult, googleOAuth] = await Promise.all([
            api("/customer/agents/manage/integrations/catalog"),
            api("/customer/agents/manage/integrations"),
            api("/customer/agents/manage/integrations/google-calendar/oauth/status").catch(() => ({ready: false})),
        ]);
        const definitions = catalogResult.integrations || [];
        const integrations = listResult.integrations || [];
        const definitionOptions = definitions.map(item =>
            `<option value="${safe(item.type)}">${safe(item.name || item.type)}</option>`
        ).join("");
        const oauthResult = new URLSearchParams(window.location.search).get("calendar_oauth");
        const oauthMessage = oauthResult === "connected"
            ? '<div class="success">Google Calendar connected and validated.</div>'
            : oauthResult === "cancelled"
                ? '<div class="muted">Google Calendar connection was cancelled.</div>'
                : "";

        target.innerHTML = `
            ${oauthMessage}
            <div class="panel" style="margin-bottom:20px">
                <div class="service-card-head">
                    <div>
                        <h2>Connected Systems</h2>
                        <p class="muted">Connect the systems your employee needs. Google Calendar uses a secure consent flow when Xvond OAuth is configured; other secrets stay encrypted and are never shown again.</p>
                    </div>
                </div>
                <div class="service-grid" style="margin-top:14px">
                    <label>Type
                        <select id="customer-integration-type">${definitionOptions}</select>
                    </label>
                    <label>Name
                        <input id="customer-integration-name" maxlength="200" placeholder="My booking system">
                    </label>
                </div>
                <div id="customer-integration-fields" class="service-grid" style="margin-top:14px"></div>
                <div class="chat-input" style="margin-top:14px">
                    <button type="button" id="customer-integration-create">Add connected system</button>
                </div>
                <div id="customer-integration-error" class="error"></div>
            </div>
            <div class="panel">
                <h2>Your connections</h2>
                ${integrations.length ? integrations.map(item => `
                    <div class="agent">
                        <div class="service-card-head">
                            <div>
                                <strong>${safe(item.name)}</strong>
                                <p>${safe(item.integration_type)} · ${item.validated ? "Validated" : item.configured ? "Configured · validation required" : "Setup incomplete"}</p>
                            </div>
                            <span class="pill">${item.enabled ? "Active" : "Inactive"}</span>
                        </div>
                        ${(item.configured_secret_fields || []).length
                            ? `<p class="muted">Protected credentials configured: ${safe((item.configured_secret_fields || []).join(", "))}</p>`
                            : ""}
                        ${Number(item.operation_count || 0) > 0
                            ? `<p class="muted">${Number(item.operation_count)} API operations imported.</p>`
                            : ""}
                        ${item.openapi_import_supported
                            ? `<button type="button" onclick="importCustomerIntegrationOpenAPI(${Number(item.id)})">Import OpenAPI</button>`
                            : ""}
                        ${item.integration_type === "calendar" && googleOAuth.ready
                            ? `<button type="button" onclick="connectGoogleCalendar(${Number(item.id)})">Reconnect Google Calendar</button>`
                            : item.configured && !item.validated
                                ? `<button type="button" onclick="validateCustomerIntegration(${Number(item.id)})">Validate connection</button>`
                                : ""}
                        <button type="button" onclick="deleteCustomerIntegration(${Number(item.id)})">Remove</button>
                    </div>
                `).join("") : '<p class="muted">No connected systems yet.</p>'}
            </div>
        `;

        const type = document.getElementById("customer-integration-type");
        const createButton = document.getElementById("customer-integration-create");
        const renderFields = () => {
            const definition = definitions.find(item => item.type === type?.value) || definitions[0];
            const host = document.getElementById("customer-integration-fields");
            if (!host) return;
            const oauthCalendar = definition?.type === "calendar" && googleOAuth.ready;
            const hiddenOAuthFields = new Set(["access_token", "refresh_token", "client_id", "client_secret"]);
            const fields = (definition?.config_fields || []).filter(
                field => !oauthCalendar || !hiddenOAuthFields.has(String(field.name || ""))
            );
            host.innerHTML = fields.map(field => {
                const choices = Array.isArray(field.choices) ? field.choices : [];
                const defaultValue = field.default == null ? "" : String(field.default);
                const control = choices.length
                    ? `<select
                            data-integration-config="${safe(field.name)}"
                            ${field.required ? "required" : ""}
                        >
                            ${choices.map(value => `
                                <option value="${safe(value)}" ${String(value) === defaultValue ? "selected" : ""}>
                                    ${safe(value)}
                                </option>
                            `).join("")}
                        </select>`
                    : `<input
                            data-integration-config="${safe(field.name)}"
                            type="${field.secret ? "password" : "text"}"
                            autocomplete="off"
                            ${field.required ? "required" : ""}
                            ${!field.secret && defaultValue ? `value="${safe(defaultValue)}"` : ""}
                            placeholder="${field.secret ? "Stored encrypted" : safe(field.label || field.name)}"
                        >`;
                return `
                    <label>
                        ${safe(field.label || field.name)}
                        ${control}
                    </label>
                `;
            }).join("");
            if (createButton) {
                createButton.textContent = oauthCalendar
                    ? "Connect Google Calendar"
                    : "Add connected system";
            }
        };
        type?.addEventListener("change", renderFields);
        renderFields();

        createButton?.addEventListener("click", async event => {
            const button = event.currentTarget;
            const error = document.getElementById("customer-integration-error");
            if (error) error.textContent = "";
            const integrationType = String(type?.value || "").trim();
            const name = String(document.getElementById("customer-integration-name")?.value || "").trim();
            const config = {};
            document.querySelectorAll("#customer-integration-fields [data-integration-config]").forEach(input => {
                const value = String(input.value || "").trim();
                if (value) config[input.dataset.integrationConfig] = value;
            });
            if (!integrationType || !name) {
                if (error) error.textContent = "Choose a type and name.";
                return;
            }
            button.disabled = true;
            try {
                if (integrationType === "calendar" && googleOAuth.ready) {
                    const result = await api("/customer/agents/manage/integrations/google-calendar/oauth/start", {
                        method: "POST",
                        body: JSON.stringify({
                            name,
                            calendar_id: config.calendar_id || "primary",
                            timezone: config.timezone || "",
                            slot_minutes: Number(config.slot_minutes || 30),
                        }),
                    });
                    window.location.assign(result.authorization_url);
                    return;
                }
                await api("/customer/agents/manage/integrations", {
                    method: "POST",
                    body: JSON.stringify({integration_type: integrationType, name, config}),
                });
                await renderIntegrations();
            } catch (err) {
                if (error) error.textContent = err?.message || "Could not add connected system.";
            } finally {
                if (document.body.contains(button)) button.disabled = false;
            }
        });
    } catch (error) {
        target.innerHTML = `<div class="panel"><p>${safe(error.message)}</p></div>`;
    }
}

async function connectGoogleCalendar(integrationId) {
    try {
        const result = await api("/customer/agents/manage/integrations/google-calendar/oauth/start", {
            method: "POST",
            body: JSON.stringify({integration_id: Number(integrationId)}),
        });
        window.location.assign(result.authorization_url);
    } catch (error) {
        alert(error.message);
    }
}

window.connectGoogleCalendar = connectGoogleCalendar;

async function validateCustomerIntegration(integrationId) {
    try {
        await api(`/customer/agents/manage/integrations/${Number(integrationId)}/validate`, {method: "POST"});
        await renderIntegrations();
    } catch (error) {
        alert(error.message);
    }
}

window.validateCustomerIntegration = validateCustomerIntegration;

async function importCustomerIntegrationOpenAPI(integrationId) {
    const url = String(prompt("OpenAPI / Swagger JSON or YAML URL") || "").trim();
    if (!url) return;
    try {
        const result = await api(`/customer/agents/manage/integrations/${Number(integrationId)}/openapi`, {
            method: "POST",
            body: JSON.stringify({url}),
        });
        alert(`Imported ${Number(result.operation_count || 0)} API operations. Validate the connection before using it.`);
        await renderIntegrations();
    } catch (error) {
        alert(error.message);
    }
}

window.importCustomerIntegrationOpenAPI = importCustomerIntegrationOpenAPI;

async function deleteCustomerIntegration(integrationId) {
    if (!confirm("Remove this connected system?")) return;
    try {
        await api(`/customer/agents/manage/integrations/${Number(integrationId)}`, {method: "DELETE"});
        await renderIntegrations();
    } catch (error) {
        alert(error.message);
    }
}

window.deleteCustomerIntegration = deleteCustomerIntegration;


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
    setCustomerNavigationOpen(false);
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
    if (loader === "business") {
        await loadCustomerBusiness({
            pageId: item.id || "business",
            capabilityModule: item.capability_module || null,
            includeHandoffs: item.include_handoffs !== false && !item.capability_module,
            label: item.label || "Operations",
        });
    }
    if (loader === "integrations") await renderIntegrations();
    if (loader === "billing") renderBilling();
    if (loader === "service") renderServicePage(item.service_code, item.id);
}

async function loadCustomerBusiness(options = {}) {
    const pageId = options.pageId || "business";
    const capabilityModule = String(options.capabilityModule || "").trim();
    const includeHandoffs = options.includeHandoffs !== false;
    const label = options.label || (capabilityModule ? capabilityModule.replaceAll("_", " ") : "Operations");
    const page = document.getElementById(`page-${pageId}`);
    const target = pageId === "business"
        ? document.getElementById("customer-business-content")
        : page?.querySelector(".dynamic-page-content");
    if (!target) return;

    try {
        const operationPath = capabilityModule
            ? `/customer/action-requests?module=${encodeURIComponent(capabilityModule)}`
            : "/customer/action-requests";
        const [operationResult, handoffs] = await Promise.all([
            api(operationPath),
            includeHandoffs ? api("/customer/business/handoffs") : Promise.resolve([])
        ]);
        const operations = operationResult.requests || [];
        const open = operations.filter(x => !["completed", "cancelled"].includes(x.status));
        const completed = operations.filter(x => x.status === "completed");

        if (pageId === "business") {
            const totalEl = document.getElementById("customer-operations-count");
            const openEl = document.getElementById("customer-open-count");
            const completedEl = document.getElementById("customer-completed-count");
            const handoffsEl = document.getElementById("customer-handoffs-count");
            if (totalEl) totalEl.textContent = operations.length;
            if (openEl) openEl.textContent = open.length;
            if (completedEl) completedEl.textContent = completed.length;
            if (handoffsEl) handoffsEl.textContent = handoffs.length;
        }

        const metrics = capabilityModule ? `
            <div class="cards" style="margin-bottom:20px">
                <div class="card"><span>Total</span><strong>${operations.length}</strong></div>
                <div class="card"><span>Open</span><strong>${open.length}</strong></div>
                <div class="card"><span>Completed</span><strong>${completed.length}</strong></div>
            </div>
        ` : "";

        target.innerHTML = `
            ${metrics}
            ${customerBusinessSection(label, operations, item => `
                <div class="service-card-head">
                    <div>
                        <strong>${safe(item.action_label || (item.action_type || "operation").replaceAll("_", " "))} #${item.id}</strong>
                        <p class="muted">${safe(item.summary || "")}</p>
                    </div>
                    <span class="pill">${safe(item.status)}</span>
                </div>
                ${operationDetails(item.details)}
                ${operationButtons(item)}
            `)}
            ${includeHandoffs ? customerBusinessSection("Human Handoffs", handoffs, item => `
                <strong>Handoff #${item.id}</strong>
                <p>Reason: ${safe(item.reason || "-")}</p>
                <p>Department: ${safe(item.department || "-")}</p>
                <p>Priority: ${safe(item.priority || "-")}</p>
                <p>Status: ${safe(item.status)}</p>
            `) : ""}
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
    if (item.external_execution?.reconciliation_required) {
        return `
            <div class="note">
                <strong>External result needs reconciliation</strong>
                <p class="muted">Xvond will not retry this action automatically because the external system outcome is not certain. Check the connected system, then confirm what actually happened.</p>
                ${item.external_execution?.error ? `<p class="error">${safe(item.external_execution.error)}</p>` : ""}
                <div class="chat-input">
                    <button onclick="reconcileCustomerOperation(${Number(item.id)},'executed')">Confirm executed</button>
                    <button onclick="reconcileCustomerOperation(${Number(item.id)},'not_executed')">Confirm not executed</button>
                    <button onclick="reconcileCustomerOperation(${Number(item.id)},'cancelled')">Confirm cancelled</button>
                </div>
            </div>
        `;
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

async function reconcileCustomerOperation(id, outcome) {
    const labels = {
        executed: "executed successfully",
        not_executed: "not executed",
        cancelled: "cancelled",
    };
    const label = labels[outcome] || outcome;
    if (!confirm(`Confirm that this external operation was ${label}?`)) return;
    const note = prompt("Optional reconciliation note:", "") || "";
    try {
        await api(`/customer/action-requests/${id}/reconcile`, {
            method: "PATCH",
            body: JSON.stringify({outcome, note})
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

initializeCustomerShell();
if (token) startPortal();
