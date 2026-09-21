(() => {
    function optionList(values, selected, placeholder) {
        const list = Array.isArray(values) ? values : [];
        const options = [`<option value="">${safe(placeholder)}</option>`];
        for (const value of list) {
            options.push(`<option value="${safe(value)}" ${String(value) === String(selected || "") ? "selected" : ""}>${safe(value)}</option>`);
        }
        if (selected && !list.includes(selected)) {
            options.push(`<option value="${safe(selected)}" selected>${safe(selected)}</option>`);
        }
        return options.join("");
    }

    async function renderStructuredBusinessProfile(target) {
        try {
            const d = await api("/customer/agents/manage/business-information");
            customerBusinessProfileCache = d;
            const catalog = d.catalog || {};
            target.innerHTML = `
                <div class="service-card" style="margin-bottom:18px">
                    <div class="service-card-head">
                        <div><h3>Company Identity</h3><p>Core facts used across every AI employee and customer channel.</p></div>
                        <span class="pill">Shared</span>
                    </div>
                    <div class="form-group"><label>Business Name</label><input id="cb-name" value="${safe(d.company_name || "")}"></div>
                    <div class="form-group"><label>Business Type</label><select id="cb-type">${optionList(catalog.business_types, d.business_type, "Select business type")}</select></div>
                    <div class="form-group"><label>Business Description</label><textarea id="cb-description" placeholder="What the company does, who it serves and what makes it different">${safe(d.description || "")}</textarea></div>
                </div>

                <div class="service-card" style="margin-bottom:18px">
                    <div class="service-card-head"><div><h3>Region & Language</h3><p>Controls dates, times, currency context and default communication behavior.</p></div></div>
                    <div class="form-grid two">
                        <div class="form-group"><label>Country</label><select id="cb-country">${optionList(catalog.countries, d.country, "Select country")}</select></div>
                        <div class="form-group"><label>Currency</label><select id="cb-currency">${optionList(catalog.currencies, d.currency, "Select currency")}</select></div>
                        <div class="form-group"><label>Timezone</label><select id="cb-timezone">${optionList(catalog.timezones, d.timezone, "Select timezone")}</select></div>
                        <div class="form-group"><label>Primary Language</label><select id="cb-primary-language">${optionList(catalog.languages, d.primary_language, "Select language")}</select></div>
                    </div>
                    <div class="form-group"><label>Additional Languages</label><input id="cb-additional-languages" value="${safe((d.additional_languages || []).join(", "))}" placeholder="English, Arabic"><small>Comma-separated. The primary language does not need to be repeated.</small></div>
                </div>

                <div class="service-card" style="margin-bottom:18px">
                    <div class="service-card-head"><div><h3>Customer Contact</h3><p>Official contact details the AI employee may use when relevant.</p></div></div>
                    <div class="form-grid two">
                        <div class="form-group"><label>Phone</label><input id="cb-phone" type="tel" value="${safe(d.phone || "")}" placeholder="+968..."></div>
                        <div class="form-group"><label>Email</label><input id="cb-email" type="email" value="${safe(d.email || "")}" placeholder="support@example.com"></div>
                    </div>
                    <div class="form-group"><label>Website</label><input id="cb-website" type="url" value="${safe(d.website || "")}" placeholder="https://example.com"></div>
                </div>

                <div class="service-card" style="margin-bottom:18px">
                    <div class="service-card-head"><div><h3>Working Hours</h3><p>Enable open days and set the normal opening and closing time.</p></div></div>
                    ${workingHoursMarkup(d.working_hours)}
                </div>

                <div class="service-card" style="margin-bottom:18px">
                    <div class="service-card-head"><div><h3>Business Information</h3><p>Operational facts synchronized into protected shared AI knowledge.</p></div></div>
                    <div class="form-grid two">
                        <div class="form-group"><label>Services / Products</label><textarea id="cb-services" placeholder="One item per line">${safe(listToLines(d.services))}</textarea><small>What customers can buy, book or ask about.</small></div>
                        <div class="form-group"><label>Locations / Branches</label><textarea id="cb-locations" placeholder="One location per line">${safe(listToLines(d.locations))}</textarea><small>Physical or service locations.</small></div>
                        <div class="form-group"><label>Service Areas</label><textarea id="cb-areas" placeholder="One area per line">${safe(listToLines(d.service_areas))}</textarea><small>Areas where the business delivers or serves customers.</small></div>
                        <div class="form-group"><label>Policies</label><textarea id="cb-policies" placeholder="One policy per line">${safe(listToLines(d.policies))}</textarea><small>Cancellation, returns, delivery, payment or other customer policies.</small></div>
                    </div>
                    <div class="form-group"><label>Business Rules</label><textarea id="cb-rules" placeholder="One rule per line">${safe(listToLines(d.business_rules))}</textarea><small>Operational rules the AI must consistently follow.</small></div>
                </div>

                <div id="cb-message" class="error"></div>
                <button onclick="saveCustomerBusinessInformation()">Save Business Profile</button>
            `;
        } catch (err) {
            target.innerHTML = `<div class="error">${safe(err.message)}</div>`;
        }
    }

    window.renderCustomerBusinessTab = renderStructuredBusinessProfile;

    const baseSaveBusiness = window.saveCustomerBusinessInformation;
    if (typeof baseSaveBusiness === "function") {
        window.saveCustomerBusinessInformation = async function structuredBusinessSave() {
            const value = id => document.getElementById(id)?.value?.trim() || "";
            if (customerBusinessProfileCache) {
                customerBusinessProfileCache.country = value("cb-country") || null;
                customerBusinessProfileCache.currency = value("cb-currency") || null;
                customerBusinessProfileCache.timezone = value("cb-timezone") || null;
                customerBusinessProfileCache.primary_language = value("cb-primary-language") || null;
                customerBusinessProfileCache.additional_languages = value("cb-additional-languages")
                    .split(",")
                    .map(item => item.trim())
                    .filter(Boolean);
            }
            return baseSaveBusiness.apply(this, arguments);
        };
    }

    async function renderBusinessProfilePage() {
        const target = document.getElementById("customer-business-profile-content");
        if (!target) return;
        target.innerHTML = '<p class="muted">Loading business profile...</p>';
        await renderStructuredBusinessProfile(target);
    }

    function accountPageRefresh() {
        if (typeof renderAccountInfo === "function") renderAccountInfo();
    }

    const baseRenderPaymentMethod = window.renderPaymentMethod;
    if (typeof baseRenderPaymentMethod === "function") {
        window.renderPaymentMethod = function truthfulBillingMethod() {
            const billing = portalOverview?.billing || {};
            if (!billing.online_payments_enabled) {
                return '<div class="billing-row"><span>Billing collection</span><strong>Managed by Xvond</strong></div>';
            }
            return baseRenderPaymentMethod.apply(this, arguments);
        };
    }

    function renderOperationalOverview() {
        const summary = portalOverview?.summary || {};
        const cardTarget = document.getElementById("dashboard-cards");

        if (currentUser?.role === "employee") {
            if (cardTarget) {
                const cards = [
                    ["Active AI Employees", summary.active_agents || 0],
                    ["Connected Channels", summary.active_channels || 0],
                    ["Conversations", summary.conversations || 0],
                    ["Human Active", summary.active_handoffs || 0],
                ];
                cardTarget.innerHTML = cards.map(([label, value]) => `
                    <div class="card"><span>${safe(label)}</span><strong>${safe(value)}</strong></div>
                `).join("");
            }
            const serviceTarget = document.getElementById("dashboard-services");
            if (serviceTarget) {
                serviceTarget.innerHTML = '<p class="muted">Operator access: handle Customer Inbox conversations and human takeover. Company configuration, AI settings, team administration and billing remain manager-only.</p>';
            }
            return;
        }

        const services = activeServices();
        const hasAI = services.some(item => item.service_code === "ai_agents");
        if (!hasAI) return;

        if (cardTarget) {
            const cards = [
                ["Active AI Employees", `${summary.active_agents || 0} / ${summary.agents || 0}`],
                ["Open Operations", summary.open_operations || 0],
                ["Human Handoffs", summary.active_handoffs || 0],
                ["Unread Notifications", summary.unread_notifications || 0],
            ];
            cardTarget.innerHTML = cards.map(([label, value]) => `
                <div class="card"><span>${safe(label)}</span><strong>${safe(value)}</strong></div>
            `).join("");
        }

        const dashboard = document.getElementById("page-dashboard");
        if (!dashboard) return;
        let health = document.getElementById("dashboard-operational-health");
        if (!health) {
            health = document.createElement("div");
            health.id = "dashboard-operational-health";
            health.className = "panel";
            health.style.marginBottom = "22px";
            const servicesPanel = document.getElementById("dashboard-services")?.closest(".panel");
            if (servicesPanel) dashboard.insertBefore(health, servicesPanel);
            else dashboard.appendChild(health);
        }
        const failed = Number(summary.failed_ai_requests_24h || 0);
        const limits = Number(summary.service_limit_warnings || 0);
        const activeChannels = Number(summary.active_channels || 0);
        health.innerHTML = `
            <div class="service-card-head"><div><h2>Operational Health</h2><p>What needs attention in the live customer operation.</p></div><span class="pill">${failed + limits ? "Review" : "Healthy"}</span></div>
            <div class="billing-row"><span>Active customer channels</span><strong>${safe(activeChannels)}</strong></div>
            <div class="billing-row"><span>Failed AI requests · last 24h</span><strong>${safe(failed)}</strong></div>
            <div class="billing-row"><span>Service limits reached</span><strong>${safe(limits)}</strong></div>
        `;

        let quick = document.getElementById("dashboard-quick-actions");
        if (!quick) {
            quick = document.createElement("div");
            quick.id = "dashboard-quick-actions";
            quick.className = "panel";
            quick.style.marginBottom = "22px";
            dashboard.insertBefore(quick, health);
        }
        const navIds = new Set((portalNavigation || []).map(item => item.id));
        const action = (page, label) => navIds.has(page)
            ? `<button type="button" onclick="openPage('${page}', [...document.querySelectorAll('#portal-nav .nav-item')].find(x=>x.dataset.page==='${page}')||null)">${safe(label)}</button>`
            : "";
        quick.innerHTML = `
            <div class="service-card-head">
                <div>
                    <h2>Quick Actions</h2>
                    <p>Everything a manager uses regularly, from one workspace.</p>
                </div>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
                ${action("agents", "Manage AI Employees")}
                ${action("channels", "Manage Channels")}
                ${action("conversations", "Open Inbox")}
                ${action("customers", "Customers")}
                ${action("business-profile", "Business Profile")}
                ${action("integrations", "Connected Systems")}
                ${action("users", "Team & Access")}
                ${action("billing", "Billing")}
            </div>
        `;

        let workspace = document.getElementById("dashboard-workspace-map");
        if (!workspace) {
            workspace = document.createElement("div");
            workspace.id = "dashboard-workspace-map";
            workspace.className = "panel";
            workspace.style.marginBottom = "22px";
            const servicesPanel = document.getElementById("dashboard-services")?.closest(".panel");
            if (servicesPanel) dashboard.insertBefore(workspace, servicesPanel);
            else dashboard.appendChild(workspace);
        }
        workspace.innerHTML = `
            <div class="service-card-head">
                <div>
                    <h2>Customer Control Center</h2>
                    <p>Your company configuration is separated clearly so each setting has one owner.</p>
                </div>
            </div>
            <div class="service-grid" style="margin-top:12px">
                <div class="agent"><strong>AI Workforce</strong><p class="muted">Employees, behavior, knowledge, channels and testing.</p></div>
                <div class="agent"><strong>Operations</strong><p class="muted">Inbox, customers, bookings, orders, leads, support requests and notifications.</p></div>
                <div class="agent"><strong>Systems & Data</strong><p class="muted">Connected systems, automation, analytics, usage and operational health.</p></div>
                <div class="agent"><strong>Company & Account</strong><p class="muted">Business profile, team access, security, plan and billing.</p></div>
            </div>
        `;
    }

    const baseRenderDashboard = window.renderDashboard;
    if (typeof baseRenderDashboard === "function") {
        window.renderDashboard = function structuredDashboard() {
            const result = baseRenderDashboard.apply(this, arguments);
            renderOperationalOverview();
            return result;
        };
    }

    const baseFallbackNavigation = window.fallbackPortalNavigation;
    if (typeof baseFallbackNavigation === "function") {
        window.fallbackPortalNavigation = function structuredFallbackNavigation() {
            if (currentUser?.role === "employee") {
                return [
                    {id: "dashboard", label: "Overview", loader: "dashboard", group: "Workspace"},
                    {id: "conversations", label: "Customer Inbox", loader: "conversations", group: "Operations"},
                ];
            }
            return baseFallbackNavigation.apply(this, arguments);
        };
    }

    if (typeof window.customerRoleLabel === "function") {
        const baseRoleLabel = window.customerRoleLabel;
        window.customerRoleLabel = function structuredRoleLabel(role) {
            if (role === "employee") return "Staff Operator";
            return baseRoleLabel(role);
        };
    }

    if (typeof window.customerAssignableRoleOptions === "function") {
        window.customerAssignableRoleOptions = function structuredAssignableRoles() {
            const options = [["employee", "Staff Operator — Overview + Customer Inbox"]];
            if (["owner", "admin"].includes(currentUser?.role)) {
                options.push(["manager", "Manager — Company management access"]);
            }
            return options;
        };
    }

    const baseLoadCompanyUsers = window.loadCompanyUsers;
    if (typeof baseLoadCompanyUsers === "function") {
        window.loadCompanyUsers = async function structuredCompanyUsers() {
            const result = await baseLoadCompanyUsers.apply(this, arguments);
            const page = document.getElementById("page-users");
            const intro = page?.querySelector(".dynamic-page-content .panel .muted");
            if (intro) {
                intro.textContent = "Staff Operators can use Overview and Customer Inbox for human takeover without access to company configuration or billing. Managers can manage company workspaces and Staff Operator accounts, but not Owner or Company Admin accounts.";
            }
            return result;
        };
    }

    const baseOpenPage = window.openPage;
    if (typeof baseOpenPage === "function") {
        window.openPage = async function xvondStructuredOpenPage(name, button) {
            const result = await baseOpenPage.apply(this, arguments);
            const item = (portalNavigation || []).find(entry => entry.id === name) || {};
            const loader = item.loader || name;
            if (loader === "business-profile") await renderBusinessProfilePage();
            if (loader === "account") accountPageRefresh();
            if (loader === "dashboard") renderOperationalOverview();
            return result;
        };
    }

    function openBusinessProfileFromEmployee() {
        const button = [...document.querySelectorAll("#portal-nav .nav-item")]
            .find(item => item.dataset.page === "business-profile");
        if (typeof window.openPage === "function") {
            window.openPage("business-profile", button || null);
        }
    }
    window.openBusinessProfileFromEmployee = openBusinessProfileFromEmployee;

    const baseOpenManagerTab = window.openCustomerManagerTab;
    if (typeof baseOpenManagerTab === "function") {
        window.openCustomerManagerTab = async function structuredManagerTab(tab) {
            if (tab === "business") {
                openBusinessProfileFromEmployee();
                return;
            }
            return baseOpenManagerTab.apply(this, arguments);
        };
    }

    const baseOpenAgentSettings = window.openCustomerAgentSettings;
    if (typeof baseOpenAgentSettings === "function") {
        window.openCustomerAgentSettings = async function structuredAgentSettings(agentId) {
            const result = await baseOpenAgentSettings.apply(this, arguments);
            const target = document.getElementById("customer-agent-settings");
            if (!target) return result;

            const description = target.querySelector(".service-card-head p");
            if (description) {
                description.textContent = "Manage this employee's behavior and employee-specific knowledge. Company facts are managed once in Business Profile.";
            }

            [...target.querySelectorAll("button")].forEach(button => {
                const onclick = button.getAttribute("onclick") || "";
                if (onclick.includes("openCustomerManagerTab('business')") || button.textContent.trim() === "Business Information") {
                    button.remove();
                }
            });

            const tabArea = target.querySelector("#customer-manager-tab")?.previousElementSibling;
            if (tabArea && !target.querySelector(".business-profile-source-note")) {
                const note = document.createElement("div");
                note.className = "business-profile-source-note muted";
                note.style.margin = "8px 0 14px";
                note.innerHTML = 'Need to change services, hours, branches, contact details, policies or business rules? <button type="button" class="table-button" onclick="openBusinessProfileFromEmployee()">Open Business Profile</button>';
                tabArea.insertAdjacentElement("afterend", note);
            }
            return result;
        };
    }

    window.renderBusinessProfilePage = renderBusinessProfilePage;
})();