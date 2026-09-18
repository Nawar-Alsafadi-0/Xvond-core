(() => {
    const CHANNELS = [
        ["xvond", "Xvond Workspace"],
        ["website", "Website Chat"],
        ["whatsapp", "WhatsApp"],
        ["voice", "Voice"],
        ["telegram", "Telegram"],
        ["custom", "Custom Channel"],
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
                    <p class="employee-builder-lead">Describe any job, business role, personal agent, assistant or recurring task. Xvond saves the brief first, then builds the employee around that job.</p>
                    <p class="muted">The brief is not limited to predefined agent types or capability categories.</p>
                    <div class="employee-builder-actions">
                        <a href="/build"><button type="button">Build on Xvond.com</button></a>
                    </div>
                </div>
            </div>
        `;
    }

    function missingMarkup(items) {
        if (!(items || []).length) return '<p class="muted">Nothing else is needed from you right now.</p>';
        return `<div class="employee-builder-missing">${items.map(item => badge(String(item).replaceAll("_", " "), "setup")).join("")}</div>`;
    }

    function requirementStatusLabel(item) {
        const status = String(item?.status || "xvond_build");
        if (status === "available") return "ready";
        if (status === "xvond_managed") return item.execution_status === "disabled" ? "action paused" : "execution setup pending";
        if (status === "xvond_build") return "Xvond builds this";
        if (status === "connection_required") return "connect account";
        if (status === "customer_input_required") return "add required data";
        return status.replaceAll("_", " ");
    }

    function requirementMarkup(items) {
        if (!(items || []).length) return '<p class="muted">No extra systems were identified.</p>';
        return `<div class="employee-builder-missing">${items.map(item => {
            const status = String(item.status || "xvond_build");
            const tone = status === "available" ? "ready" : "setup";
            const label = `${String(item.key || "requirement").replaceAll("_", " ")} · ${requirementStatusLabel(item)}`;
            return badge(label, tone);
        }).join("")}</div>`;
    }

    function selfServiceMarkup(employee) {
        if (employee.delivery_mode !== "self_service") return "";
        const state = employee.self_service_readiness || {};
        const subscription = state.subscription || {};
        const limit = state.channel_limit == null ? "—" : String(state.channel_limit);
        const used = Number(state.channel_slots_used || 0);
        const blockers = (state.blockers || []).map(item => \`<li>\${escapeHtml(item)}</li>\`).join("");
        const modeLabels = {
            personal: "Personal agent",
            background: "Background worker",
            customer_facing: "Customer-facing employee",
            hybrid: "Hybrid employee",
            workspace: "Workspace employee",
        };
        return \`
            <div class="panel">
                <div class="employee-builder-kicker">SELF-SERVICE DELIVERY</div>
                <div class="employee-builder-summary-grid">
                    <div>
                        <h3>Work mode</h3>
                        <div class="employee-builder-missing">\${badge(modeLabels[state.mode] || state.mode || "Pending")}</div>
                        <p class="muted">\${state.channels_required ? "This job needs a communication channel." : "This job can run with 0 communication channels."}</p>
                    </div>
                    <div>
                        <h3>Channel slots</h3>
                        <div class="employee-builder-missing">\${badge(\`\${used}/\${limit}\`, used > 0 ? "neutral" : "ready")}</div>
                        <p class="muted">Communication channels use plan slots. Gmail, email actions, Instagram publishing and other connected systems are integrations, not channel slots.</p>
                    </div>
                    <div>
                        <h3>Subscription</h3>
                        <div class="employee-builder-missing">\${badge(subscription.active ? (subscription.plan_name || "Active") : "Subscription required", subscription.active ? "ready" : "setup")}</div>
                    </div>
                </div>
                \${blockers ? \`
                    <div class="employee-builder-section">
                        <h3>Before launch</h3>
                        <ul>\${blockers}</ul>
                    </div>
                \` : \`
                    <div class="employee-builder-section">
                        <p class="muted">Everything required for this self-service employee is ready.</p>
                    </div>
                \`}
                \${employee.can_launch ? \`
                    <div class="employee-builder-actions">
                        <button type="button" id="launch-employee-btn">Launch employee</button>
                    </div>
                    <div id="launch-employee-error" class="error"></div>
                \` : ""}
            </div>
        \`;
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
                <div class="employee-builder-kicker">EMPLOYEE BUILD PLAN</div>
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
                    <p class="muted">Xvond prepares the action plan and handles execution setup. An action plan alone does not mean tasks are running. You only need to connect external accounts, grant permissions or provide required data when the job depends on them.</p>
                </div>

                <div class="employee-builder-section">
                    <h3>Permissions</h3>
                    <div class="employee-builder-missing">${permissions}</div>
                </div>

                ${questions ? `
                    <div class="employee-builder-section">
                        <h3>Needed from you</h3>
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
        const provisioned = employee.compiled_spec?.delivery?.provisioning_version === 1;
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
                        <p class="muted">This brief is the source of truth. Xvond builds the employee around the requested job instead of limiting it to a predefined agent type.</p>
                    </div>

                    <div class="employee-builder-summary-grid">
                        <div>
                            <h3>Communication channels</h3>
                            <div class="employee-builder-missing">${channels}</div>
                            <p class="muted">Optional unless this employee needs to talk with customers or users.</p>
                        </div>
                        <div>
                            <h3>Preparation</h3>
                            <div class="employee-builder-missing">${badge(provisioned ? "Action plan prepared" : "Preparation pending", provisioned ? "ready" : "setup")}</div>
                        </div>
                    </div>

                    ${provisioned ? "" : employee.can_compile ? `
                        <div class="employee-builder-section">
                            <h3>Build this employee</h3>
                            <p class="muted">Xvond will understand the complete job, break it into tasks, compose any missing digital capabilities and identify only the external accounts, permissions or data it needs from you.</p>
                            <div class="employee-builder-actions">
                                <button type="button" id="prepare-employee-btn">Build employee</button>
                            </div>
                            <div id="prepare-employee-error" class="error"></div>
                        </div>
                    ` : `
                        <div class="employee-builder-section">
                            <h3>Next step</h3>
                            <p class="muted">Subscribe to build, test and launch this employee. Saving the Job Brief itself used no paid AI.</p>
                        </div>
                    `}

                    <div class="employee-builder-section">
                        <h3>Needed from you</h3>
                        ${missingMarkup(employee.missing_information)}
                    </div>
                </div>

                ${compiledMarkup(employee.compiled_spec)}
                ${selfServiceMarkup(employee)}

                ${employee.enabled ? `
                    <div class="panel">
                        <div class="employee-builder-kicker">LIVE</div>
                        <h2>Your employee is launched</h2>
                        <p class="muted">Manage conversations, usage, knowledge, tools, automations and connected channels from the workspace.</p>
                    </div>
                ` : `
                    <div class="panel">
                        <div class="employee-builder-kicker">DRAFT</div>
                        <h2>${employee.compiled ? "Your employee build plan is ready" : "Your job brief is saved"}</h2>
                        <p class="muted">${employee.compiled ? "Xvond owns the capability build. Finish only the external account connections, permissions or data the job needs before live actions." : "No paid AI is used while saving the brief. Subscribe before AI-backed building, testing or live execution."}</p>
                    </div>
                `}
            </div>
        `;

        const prepareButton = document.getElementById("prepare-employee-btn");
        if (prepareButton) {
            prepareButton.addEventListener("click", () => prepareEmployee(employee.agent_id));
        }
        const launchButton = document.getElementById("launch-employee-btn");
        if (launchButton) {
            launchButton.addEventListener("click", () => launchEmployee(employee.agent_id));
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
            if (error) error.textContent = err?.message || "Could not build employee.";
        } finally {
            if (button) button.disabled = false;
        }
    }

    async function launchEmployee(agentId) {
        const button = document.getElementById("launch-employee-btn");
        const error = document.getElementById("launch-employee-error");
        if (button) button.disabled = true;
        if (error) error.textContent = "";
        try {
            await api(\`/customer/employee-builder/\${agentId}/launch\`, {
                method: "POST",
                body: "{}"
            });
            await loadEmployeeBuilder();
        } catch (err) {
            if (error) error.textContent = err?.message || "Could not launch employee.";
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
