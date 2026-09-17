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
                    <h2>You do not have an AI employee yet</h2>
                    <p class="employee-builder-lead">Employee creation starts on Xvond.com, not inside the customer portal.</p>
                    <p class="muted">Build and save your employee for free. AI usage starts only after you subscribe and launch it.</p>
                    <div class="employee-builder-actions">
                        <a href="/build"><button type="button">Build on Xvond.com</button></a>
                    </div>
                </div>
            </div>
        `;
    }

    function missingMarkup(items) {
        if (!(items || []).length) return '<p class="muted">No additional setup detected.</p>';
        return `<div class="employee-builder-missing">${items.map(item => badge(String(item).replaceAll("_", " "), "setup")).join("")}</div>`;
    }

    function renderCurrent(employee) {
        const target = root();
        if (!target) return;
        const lifecycleTone = employee.enabled ? "ready" : "setup";
        const capabilities = (employee.capabilities || []).map(item => badge(labelFor(CAPABILITIES, item))).join("") || badge("Custom");
        const channels = (employee.requested_channels || []).map(item => badge(labelFor(CHANNELS, item))).join("") || '<span class="muted">No channel selected yet.</span>';

        target.innerHTML = `
            <div class="employee-builder-shell">
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
                            <div class="employee-builder-missing">${capabilities}</div>
                        </div>
                        <div>
                            <h3>Channels</h3>
                            <div class="employee-builder-missing">${channels}</div>
                        </div>
                    </div>

                    <div class="employee-builder-section">
                        <h3>Next setup</h3>
                        ${missingMarkup(employee.missing_information)}
                    </div>
                </div>

                ${employee.enabled ? `
                    <div class="panel">
                        <div class="employee-builder-kicker">LIVE</div>
                        <h2>Your employee is launched</h2>
                        <p class="muted">Manage conversations, usage, knowledge and connected channels from the workspace.</p>
                    </div>
                ` : `
                    <div class="panel">
                        <div class="employee-builder-kicker">LAUNCH</div>
                        <h2>Your employee is saved as a Draft</h2>
                        <p class="muted">Building is free. No AI is used while the employee stays in the build stage. Subscribe before testing or launching the employee.</p>
                    </div>
                `}
            </div>
        `;
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
