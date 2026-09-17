(() => {
    const CAPABILITIES = [
        ["customer_support", "Customer replies"],
        ["sales", "Sales & follow-up"],
        ["lead_capture", "Lead capture"],
        ["booking", "Bookings"],
        ["orders", "Orders"],
        ["web_research", "Web research"],
        ["email", "Email"],
        ["files", "Files"],
        ["content", "Content"],
        ["scheduling", "Scheduled tasks"],
        ["custom_task", "Custom task"],
    ];
    const CHANNELS = [
        ["xvond", "Inside Xvond"],
        ["website", "Website"],
        ["whatsapp", "WhatsApp"],
        ["instagram", "Instagram"],
        ["email", "Email"],
    ];
    const SUGGESTIONS = [
        "Respond to customers",
        "Follow up sales leads",
        "Book appointments",
        "Search the web",
        "Work with files",
        "Create content",
        "Handle email",
        "Run scheduled tasks",
    ];

    let currentEmployee = null;
    let previewState = null;

    function root() {
        return document.querySelector("#page-employee-builder .dynamic-page-content");
    }

    function escapeHtml(value) {
        if (typeof window.safe === "function") return window.safe(value);
        const element = document.createElement("div");
        element.textContent = value ?? "";
        return element.innerHTML;
    }

    function labelFor(collection, key) {
        return collection.find(([value]) => value === key)?.[1] || String(key || "").replaceAll("_", " ");
    }

    function statusLabel(status) {
        return {
            ready: "Ready",
            conversational_ready: "Ready to talk",
            connect_required: "Connect required",
            setup_required: "Setup required",
            planned: "Coming next",
        }[status] || String(status || "").replaceAll("_", " ");
    }

    function badge(value, tone = "neutral") {
        return `<span class="employee-builder-badge employee-builder-badge-${tone}">${escapeHtml(value)}</span>`;
    }

    function errorMessage(error) {
        return escapeHtml(error?.message || "Something went wrong.");
    }

    function renderLoading() {
        const target = root();
        if (!target) return;
        target.innerHTML = `
            <div class="panel employee-builder-shell">
                <div class="employee-builder-loading">Loading your AI employee…</div>
            </div>
        `;
    }

    function renderCreate() {
        const target = root();
        if (!target) return;
        target.innerHTML = `
            <div class="employee-builder-shell">
                <div class="panel employee-builder-hero">
                    <div class="employee-builder-kicker">XVOND EMPLOYEE BUILDER</div>
                    <h2>Build your AI employee</h2>
                    <p class="employee-builder-lead">One employee, with the skills and channels you choose.</p>
                    <p class="muted" dir="rtl">شو بدك موظفك يعمل؟ اكتبها بطريقتك، وXvond بتحوّلها لإعدادات واضحة.</p>

                    <label for="employee-builder-description">What do you want your employee to do?</label>
                    <textarea id="employee-builder-description" class="employee-builder-description" rows="7" placeholder="Example: Respond to my customers, know our services and prices, follow up leads, book appointments, and work on WhatsApp and Instagram."></textarea>

                    <div class="employee-builder-chips" id="employee-builder-suggestions"></div>
                    <div class="employee-builder-actions">
                        <button id="employee-builder-preview-button" type="button">Preview my employee</button>
                    </div>
                    <div id="employee-builder-create-error" class="error"></div>
                </div>

                <div class="employee-builder-principles">
                    <div><strong>One employee</strong><span>Not separate sales, support, or booking bots.</span></div>
                    <div><strong>Your instructions</strong><span>You describe the job. Xvond structures it.</span></div>
                    <div><strong>Safe by default</strong><span>External actions stay gated until their setup is complete.</span></div>
                </div>
            </div>
        `;

        const suggestions = document.getElementById("employee-builder-suggestions");
        for (const suggestion of SUGGESTIONS) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "employee-builder-chip";
            button.textContent = suggestion;
            button.addEventListener("click", () => addSuggestion(suggestion));
            suggestions?.appendChild(button);
        }
        document.getElementById("employee-builder-preview-button")?.addEventListener("click", previewEmployee);
    }

    function addSuggestion(suggestion) {
        const input = document.getElementById("employee-builder-description");
        if (!input) return;
        const current = input.value.trim();
        if (current.toLowerCase().includes(suggestion.toLowerCase())) return;
        input.value = current ? `${current}${current.endsWith(".") ? "" : "."} ${suggestion}.` : `${suggestion}.`;
        input.focus();
    }

    async function previewEmployee() {
        const description = document.getElementById("employee-builder-description")?.value.trim() || "";
        const error = document.getElementById("employee-builder-create-error");
        if (error) error.textContent = "";
        if (description.length < 8) {
            if (error) error.textContent = "Describe the employee in a little more detail.";
            return;
        }

        const button = document.getElementById("employee-builder-preview-button");
        if (button) button.disabled = true;
        try {
            const result = await api("/customer/employee-builder/preview", {
                method: "POST",
                body: JSON.stringify({description}),
            });
            previewState = result;
            renderPreview(result);
        } catch (err) {
            if (error) error.textContent = err.message;
        } finally {
            if (button) button.disabled = false;
        }
    }

    function optionMarkup(items, selected, name) {
        const selectedSet = new Set(selected || []);
        return items.map(([value, label]) => `
            <label class="employee-builder-option">
                <input type="checkbox" name="${escapeHtml(name)}" value="${escapeHtml(value)}" ${selectedSet.has(value) ? "checked" : ""}>
                <span>${escapeHtml(label)}</span>
            </label>
        `).join("");
    }

    function readinessMarkup(readiness) {
        const capabilityRows = Object.entries(readiness?.capabilities || {}).map(([key, value]) => `
            <div class="employee-builder-readiness-row">
                <span>${escapeHtml(labelFor(CAPABILITIES, key))}</span>
                ${badge(statusLabel(value), value.includes("ready") ? "ready" : value === "planned" ? "planned" : "setup")}
            </div>
        `).join("");
        const channelRows = Object.entries(readiness?.channels || {}).map(([key, value]) => `
            <div class="employee-builder-readiness-row">
                <span>${escapeHtml(labelFor(CHANNELS, key))}</span>
                ${badge(statusLabel(value), value === "ready" ? "ready" : value === "planned" ? "planned" : "setup")}
            </div>
        `).join("");
        return `
            <div class="employee-builder-readiness">
                <div><h4>Capabilities</h4>${capabilityRows || '<p class="muted">No capabilities detected.</p>'}</div>
                <div><h4>Channels</h4>${channelRows || '<p class="muted">Inside Xvond</p>'}</div>
            </div>
        `;
    }

    function missingMarkup(items) {
        if (!(items || []).length) return '<p class="muted">No additional setup detected.</p>';
        return `<div class="employee-builder-missing">${items.map(item => badge(String(item).replaceAll("_", " "), "setup")).join("")}</div>`;
    }

    function renderPreview(result) {
        const target = root();
        const blueprint = result?.blueprint || {};
        if (!target) return;
        target.innerHTML = `
            <div class="employee-builder-shell">
                <div class="panel">
                    <button type="button" id="employee-builder-back" class="employee-builder-link-button">← Edit description</button>
                    <div class="employee-builder-kicker">REVIEW</div>
                    <h2>This is how Xvond understood your employee</h2>
                    <p class="muted">You are still creating one employee. These are its capabilities and places it may work.</p>

                    <label for="employee-builder-name">Employee name</label>
                    <input id="employee-builder-name" maxlength="200" value="${escapeHtml(blueprint.name || "My AI Employee")}">

                    <div class="employee-builder-review-grid">
                        <div>
                            <h3>What should it do?</h3>
                            <div class="employee-builder-options">${optionMarkup(CAPABILITIES, blueprint.capabilities, "employee-capability")}</div>
                        </div>
                        <div>
                            <h3>Where should it work?</h3>
                            <div class="employee-builder-options">${optionMarkup(CHANNELS, blueprint.channels, "employee-channel")}</div>
                        </div>
                    </div>

                    <div class="employee-builder-section">
                        <h3>Readiness</h3>
                        ${readinessMarkup(result.readiness)}
                    </div>
                    <div class="employee-builder-section">
                        <h3>Setup Xvond will ask for next</h3>
                        ${missingMarkup(blueprint.missing_information)}
                    </div>

                    <div class="employee-builder-actions">
                        <button id="employee-builder-create-button" type="button">Create my employee</button>
                    </div>
                    <div id="employee-builder-preview-error" class="error"></div>
                </div>
            </div>
        `;
        document.getElementById("employee-builder-back")?.addEventListener("click", renderCreateWithDescription);
        document.getElementById("employee-builder-create-button")?.addEventListener("click", createEmployee);
    }

    function renderCreateWithDescription() {
        const description = previewState?.blueprint?.description || "";
        renderCreate();
        const input = document.getElementById("employee-builder-description");
        if (input) input.value = description;
    }

    function checkedValues(name) {
        return Array.from(document.querySelectorAll(`input[name="${name}"]:checked`)).map(item => item.value);
    }

    async function createEmployee() {
        if (!previewState?.blueprint) return;
        const button = document.getElementById("employee-builder-create-button");
        const error = document.getElementById("employee-builder-preview-error");
        if (error) error.textContent = "";
        if (button) button.disabled = true;
        try {
            const result = await api("/customer/employee-builder/create", {
                method: "POST",
                body: JSON.stringify({
                    description: previewState.blueprint.description,
                    name: document.getElementById("employee-builder-name")?.value.trim() || "My AI Employee",
                    capabilities: checkedValues("employee-capability"),
                    channels: checkedValues("employee-channel"),
                }),
            });
            currentEmployee = {
                agent_id: result.agent_id,
                name: result.name,
                description: result.blueprint?.description || previewState.blueprint.description,
                enabled: result.enabled,
                lifecycle: result.lifecycle,
                capabilities: result.blueprint?.capabilities || [],
                requested_channels: result.blueprint?.channels || [],
                permissions: result.blueprint?.permissions || {},
                missing_information: result.blueprint?.missing_information || [],
            };
            renderCurrent(currentEmployee, true);
        } catch (err) {
            if (error) error.textContent = err.message;
        } finally {
            if (button) button.disabled = false;
        }
    }

    function renderCurrent(employee, justCreated = false) {
        const target = root();
        if (!target) return;
        const lifecycleTone = employee.enabled ? "ready" : "setup";
        target.innerHTML = `
            <div class="employee-builder-shell">
                ${justCreated ? '<div class="employee-builder-success">Your AI employee was created as a safe draft.</div>' : ""}
                <div class="panel employee-builder-current">
                    <div class="employee-builder-current-head">
                        <div>
                            <div class="employee-builder-kicker">YOUR AI EMPLOYEE</div>
                            <h2>${escapeHtml(employee.name || "My AI Employee")}</h2>
                            <p>${escapeHtml(employee.description || "")}</p>
                        </div>
                        ${badge(employee.enabled ? "Live" : "Draft", lifecycleTone)}
                    </div>

                    <div class="employee-builder-summary-grid">
                        <div>
                            <h3>Capabilities</h3>
                            <div class="employee-builder-missing">${(employee.capabilities || []).map(item => badge(labelFor(CAPABILITIES, item))).join("") || badge("Custom")}</div>
                        </div>
                        <div>
                            <h3>Requested channels</h3>
                            <div class="employee-builder-missing">${(employee.requested_channels || []).map(item => badge(labelFor(CHANNELS, item))).join("") || badge("Inside Xvond")}</div>
                        </div>
                    </div>

                    <div class="employee-builder-section">
                        <h3>Next setup</h3>
                        ${missingMarkup(employee.missing_information)}
                    </div>
                </div>

                <div class="panel employee-builder-test">
                    <div class="employee-builder-kicker">SAFE TEST</div>
                    <h2>Talk to your employee</h2>
                    <p class="muted">Draft testing cannot use external tools or customer channels.</p>
                    <div id="employee-builder-test-chat" class="employee-builder-test-chat"></div>
                    <div class="employee-builder-test-input">
                        <input id="employee-builder-test-message" placeholder="Give your employee a test task…">
                        <button id="employee-builder-test-button" type="button">Send</button>
                    </div>
                    <div id="employee-builder-test-error" class="error"></div>
                </div>
            </div>
        `;
        const testInput = document.getElementById("employee-builder-test-message");
        testInput?.addEventListener("keydown", event => {
            if (event.key === "Enter") testEmployee();
        });
        document.getElementById("employee-builder-test-button")?.addEventListener("click", testEmployee);
    }

    function appendTestMessage(role, message) {
        const chat = document.getElementById("employee-builder-test-chat");
        if (!chat) return;
        const row = document.createElement("div");
        row.className = `employee-builder-test-row ${role === "You" ? "is-user" : "is-employee"}`;
        const strong = document.createElement("strong");
        strong.textContent = role;
        const body = document.createElement("div");
        body.textContent = message;
        row.append(strong, body);
        chat.appendChild(row);
        chat.scrollTop = chat.scrollHeight;
    }

    async function testEmployee() {
        const employee = currentEmployee;
        const input = document.getElementById("employee-builder-test-message");
        const error = document.getElementById("employee-builder-test-error");
        const button = document.getElementById("employee-builder-test-button");
        const message = input?.value.trim() || "";
        if (!employee?.agent_id || !message) return;
        if (error) error.textContent = "";
        appendTestMessage("You", message);
        if (input) input.value = "";
        if (button) button.disabled = true;
        try {
            const result = await api(`/customer/employee-builder/${employee.agent_id}/test`, {
                method: "POST",
                body: JSON.stringify({message}),
            });
            appendTestMessage(employee.name || "AI Employee", result.message || "");
        } catch (err) {
            if (error) error.textContent = err.message;
        } finally {
            if (button) button.disabled = false;
        }
    }

    async function loadEmployeeBuilder() {
        renderLoading();
        try {
            const result = await api("/customer/employee-builder/current");
            currentEmployee = result.employee || null;
            previewState = null;
            if (currentEmployee) renderCurrent(currentEmployee);
            else renderCreate();
        } catch (err) {
            const target = root();
            if (target) target.innerHTML = `<div class="panel"><div class="error">${errorMessage(err)}</div></div>`;
        }
    }

    window.loadEmployeeBuilder = loadEmployeeBuilder;

    if (typeof window.openPage === "function") {
        const baseOpenPage = window.openPage;
        window.openPage = async function employeeBuilderOpenPage(name, button) {
            await baseOpenPage(name, button);
            if (name === "employee-builder") await loadEmployeeBuilder();
        };
    }
})();
