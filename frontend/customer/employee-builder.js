(() => {
    const CHANNELS = [
        ["xvond", "Xvond Workspace"],
        ["website", "Website"],
        ["whatsapp", "WhatsApp"],
        ["instagram", "Instagram"],
        ["email", "Email"],
    ];

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

    function badge(value, tone = "neutral") {
        return `<span class="employee-builder-badge employee-builder-badge-${tone}">${escapeHtml(value)}</span>`;
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

    function renderNoEmployee() {
        const target = root();
        if (!target) return;
        target.innerHTML = `
            <div class="employee-builder-shell">
                <div class="panel employee-builder-hero">
                    <div class="employee-builder-kicker">XVOND AI EMPLOYEE</div>
                    <h2>Tell Xvond what you need</h2>
                    <p class="employee-builder-lead">Describe any job, business role, personal agent, assistant or recurring task. Xvond saves the brief first, then the employee is prepared around that job.</p>
                    <p class="muted">The brief is not limited to predefined agent types or capability categories.</p>
                    <div class="employee-builder-actions">
                        <a href="/build"><button type="button">Build on Xvond.com</button></a>
                    </div>
                </div>
            </div>
        `;
    }

    function missingMarkup(items) {
        if (!(items || []).length) return '<p class="muted">No additional setup detected yet.</p>';
        return `<div class="employee-builder-missing">${items.map(item => badge(String(item).replaceAll("_", " "), "setup")).join("")}</div>`;
    }

    function requirementMarkup(items) {
        if (!(items || []).length) return '<p class="muted">No extra systems were identified.</p>';
        return `<div class="employee-builder-missing">${items.map(item => {
            const tone = item.status === "available" ? "ready" : "setup";
            const label = `${String(item.key || "requirement").replaceAll("_", " ")} · ${String(item.status || "custom_required").replaceAll("_", " ")}`;
            return badge(label, tone);
        }).join("")}</div>`;
    }

    function compiledMarkup(spec) {
        if (!spec) return "";
        const tasks = (spec.tasks || []).map(item => `
            <div class="note">
                <strong>${escapeHtml(item.name || "Task")}</strong>
                <div>${escapeHtml(item.description || "")}</div>
                <div class="muted">Trigger: ${escapeHtml(item.trigger || "as requested")}</div>
            </div>
        `).join("") || '<p class="muted">No explicit task list was returned.</p>';
        const permissions = (spec.permissions || []).map(item => badge(`${item.action}: ${String(item.mode || "ask_before").replaceAll("_", " ")}`, item.mode === "automatic" ? "ready" : "setup")).join("") || '<span class="muted">No additional permission rules.</span>';
        const questions = (spec.setup_questions || []).map(item => `<li>${escapeHtml(item)}</li>`).join("");

        return `
            <div class="panel">
                <div class="employee-builder-kicker">EMPLOYEE SPECIFICATION</div>
                <h2>${escapeHtml(spec.role || "AI Employee")}</h2>
                <p>${escapeHtml(spec.summary || "")}</p>
                <p class="muted">Scope: ${escapeHtml(spec.scope || "hybrid")}</p>

                <div class="employee-builder-section">
                    <h3>What this employee will do</h3>
                    ${tasks}
                </div>

                <div class="employee-builder-section">
                    <h3>Systems, tools and connections</h3>
                    ${requirementMarkup(spec.requirements)}
                    <p class="muted">Anything marked setup, connection or custom required is not active yet and must be configured before the employee can use it.</p>
                </div>

                <div class="employee-builder-section">
                    <h3>Permissions</h3>
                    <div class="employee-builder-missing">${permissions}</div>
                </div>

                ${questions ? `
                    <div class="employee-builder-section">
                        <h3>Needed to finish setup</h3>
                        <ul>${questions}</ul>
                    </div>
                ` : ""}
            </div>
        `;
    }

    function renderCurrent(employee) {
        const target = root();
        if (!target) return;
        const lifecycleTone = employee.enabled ? "ready" : "setup";
        const channels = (employee.requested_channels || []).map(item => badge(labelFor(CHANNELS, item))).join("") || '<span class="muted">No channel selected yet.</span>';

        target.innerHTML = `
            <div class="employee-builder-shell">
                <div class="panel employee-builder-current">
                    <div class="employee-builder-current-head">
                        <div>
                            <div class="employee-builder-kicker">YOUR XVOND EMPLOYEE</div>
                            <h2>${escapeHtml(employee.name || "My AI Employee")}</h2>
                        </div>
                        ${badge(employee.enabled ? "Live" : "Draft", lifecycleTone)}
                    </div>

                    <div class="employee-builder-section">
                        <h3>Job brief</h3>
                        <p>${escapeHtml(employee.description || "")}</p>
                        <p class="muted">This brief is the source of truth for the employee. Internal capability labels are implementation details and do not limit what the employee can be built to do.</p>
                    </div>

                    <div class="employee-builder-summary-grid">
                        <div>
                            <h3>Channels</h3>
                            <div class="employee-builder-missing">${channels}</div>
                        </div>
                        <div>
                            <h3>Preparation</h3>
                            <div class="employee-builder-missing">${badge(employee.compiled ? "Prepared" : "Job brief saved", employee.compiled ? "ready" : "setup")}</div>
                        </div>
                    </div>

                    ${employee.compiled ? "" : employee.can_compile ? `
                        <div class="employee-builder-section">
                            <h3>Prepare this employee</h3>
                            <p class="muted">Xvond will now use AI to understand the full job, identify tasks, systems, integrations, automations and permissions, and build the employee specification.</p>
                            <div class="employee-builder-actions">
                                <button type="button" id="prepare-employee-btn">Prepare employee</button>
                            </div>
                            <div id="prepare-employee-error" class="error"></div>
                        </div>
                    ` : `
                        <div class="employee-builder-section">
                            <h3>Next step</h3>
                            <p class="muted">Subscribe to prepare, test and launch this employee. Saving the Job Brief itself used no paid AI.</p>
                        </div>
                    `}

                    <div class="employee-builder-section">
                        <h3>Setup requirements</h3>
                        ${missingMarkup(employee.missing_information)}
                    </div>
                </div>

                ${compiledMarkup(employee.compiled_spec)}

                ${employee.enabled ? `
                    <div class="panel">
                        <div class="employee-builder-kicker">LIVE</div>
                        <h2>Your employee is launched</h2>
                        <p class="muted">Manage conversations, usage, knowledge, tools, automations and connected channels from the workspace.</p>
                    </div>
                ` : `
                    <div class="panel">
                        <div class="employee-builder-kicker">DRAFT</div>
                        <h2>${employee.compiled ? "Your employee is prepared" : "Your job brief is saved"}</h2>
                        <p class="muted">${employee.compiled ? "Finish the required connections and setup before enabling real external actions." : "No paid AI is used while saving the brief. Subscribe before AI-backed preparation, testing or live execution."}</p>
                    </div>
                `}
            </div>
        `;

        const prepareButton = document.getElementById("prepare-employee-btn");
        if (prepareButton) {
            prepareButton.addEventListener("click", () => prepareEmployee(employee.agent_id));
        }
    }

    async function prepareEmployee(agentId) {
        const button = document.getElementById("prepare-employee-btn");
        const error = document.getElementById("prepare-employee-error");
        if (button) button.disabled = true;
        if (error) error.textContent = "";
        try {
            await api(`/customer/employee-builder/${agentId}/compile`, {
                method: "POST",
                body: "{}"
            });
            await loadEmployeeBuilder();
        } catch (err) {
            if (error) error.textContent = err?.message || "Could not prepare employee.";
        } finally {
            if (button) button.disabled = false;
        }
    }

    async function loadEmployeeBuilder() {
        renderLoading();
        try {
            const result = await api("/customer/employee-builder/current");
            if (result.employee) renderCurrent(result.employee);
            else renderNoEmployee();
        } catch (err) {
            const target = root();
            if (target) target.innerHTML = `<div class="panel"><div class="error">${escapeHtml(err?.message || "Something went wrong.")}</div></div>`;
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
