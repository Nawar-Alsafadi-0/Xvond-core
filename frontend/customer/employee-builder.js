(() => {
    const CHANNELS = [
        ["xvond", "Xvond Workspace"],
        ["website", "Website Chat"],
        ["whatsapp", "WhatsApp"],
        ["voice", "Voice / Phone"],
        ["telegram", "Telegram"],
        ["instagram", "Instagram DM"],
        ["messenger", "Facebook Messenger"],
        ["email", "Email"],
        ["sms", "SMS"],
        ["slack", "Slack"],
        ["teams", "Microsoft Teams"],
        ["custom", "Custom / API Channel"],
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
        if (status === "xvond_managed") {
            const scheduleStatus = String(item?.schedule_status || "");
            if (scheduleStatus === "ready") return "scheduled & ready";
            if (scheduleStatus === "approval_required") return "automatic permission required";
            if (scheduleStatus === "schedule_required" || scheduleStatus === "schedule_setup_required") return "schedule setup required";
            if (scheduleStatus === "runtime_inputs_required") return "add task inputs";
            if (scheduleStatus === "disabled") return "schedule paused";
            if (item.execution_status === "ready") return "ready";
            if (item.execution_status === "disabled") return "action paused";
            return "execution setup pending";
        }
        if (status === "xvond_build") return "Xvond builds this";
        if (status === "connection_required") {
            if (item?.self_service_connection_status === "xvond_adapter_required") return "Xvond connection adapter required";
            if (item?.self_service_connection_status === "xvond_custom_provider_setup") return "Xvond custom connection setup";
            if (item?.self_service_connection_status === "xvond_managed_available") return "Xvond managed setup";
            return "connect account";
        }
        if (status === "customer_input_required") return "add required data";
        return status.replaceAll("_", " ");
    }

    function requirementMarkup(items) {
        if (!(items || []).length) return '<p class="muted">No extra systems were identified.</p>';
        return `<div class="employee-builder-missing">${items.map(item => {
            const status = String(item.status || "xvond_build");
            const ready = status === "available" || (
                status === "xvond_managed" && item.execution_status === "ready"
            );
            const tone = ready ? "ready" : "setup";
            const label = `${String(item.key || "requirement").replaceAll("_", " ")} · ${requirementStatusLabel(item)}`;
            return badge(label, tone);
        }).join("")}</div>`;
    }

    function journeyStatusLabel(status) {
        const labels = {
            complete: "Ready",
            action_required: "Your action",
            waiting: "Xvond / provider",
            blocked: "Locked",
        };
        return labels[String(status || "")] || String(status || "Pending");
    }

    function journeyTone(status) {
        if (status === "complete") return "ready";
        if (status === "action_required") return "setup";
        if (status === "waiting") return "planned";
        return "neutral";
    }

    function journeyActionMarkup(action) {
        const type = String(action?.type || "");
        if (type === "choose_plan" && !["owner", "admin"].includes(String(currentUser?.role || ""))) {
            return '<span class="muted">A company Owner or Admin must choose the plan.</span>';
        }

        if (type === "connect_system") {
            const encodedKey = encodeURIComponent(String(action?.key || ""));
            const isBooking = String(action?.key || "") === "booking";
            return `
                <div class="employee-builder-setup-answer" data-connect-system="${encodedKey}">
                    <strong>${escapeHtml(action?.label || "Connect existing system")}</strong>
                    ${action?.detail ? `<p class="muted">${escapeHtml(action.detail)}</p>` : ""}
                    <label>
                        <span>Connected system</span>
                        <select data-integration-select="${encodedKey}">
                            <option value="">Loading connections…</option>
                        </select>
                    </label>
                    <div data-integration-endpoint-fields="${encodedKey}">
                        <label>
                            <span>${isBooking ? "Create booking endpoint" : "Execute endpoint"}</span>
                            <input type="text" data-integration-execute="${encodedKey}" placeholder="/api/bookings">
                        </label>
                        ${isBooking ? `
                            <label>
                                <span>Availability endpoint</span>
                                <input type="text" data-integration-availability="${encodedKey}" placeholder="/api/availability">
                            </label>
                        ` : ""}
                        <label>
                            <span>Cancel endpoint (optional)</span>
                            <input type="text" data-integration-cancel="${encodedKey}" placeholder="/api/bookings/{id}/cancel">
                        </label>
                    </div>
                    <div class="employee-builder-actions">
                        <button type="button" data-bind-integration="${encodedKey}">Use this connection</button>
                        <button type="button" data-open-integrations>Manage connections</button>
                    </div>
                    <div class="error" data-integration-bind-error="${encodedKey}"></div>
                </div>
            `;
        }

        if (type === "provide_input") {
            const encodedKey = encodeURIComponent(String(action?.key || ""));
            const fields = Array.isArray(action?.fields) ? action.fields : [];
            return `
                <div class="employee-builder-setup-answer">
                    <strong>${escapeHtml(action?.label || "Provide required information")}</strong>
                    ${action?.detail ? `<p class="muted">${escapeHtml(action.detail)}</p>` : ""}
                    ${fields.length ? fields.map(field => {
                        const fieldKey = encodeURIComponent(String(field?.key || ""));
                        return `
                            <label>
                                <span>${escapeHtml(field?.label || field?.key || "Required field")}</span>
                                ${field?.detail ? `<small class="muted">${escapeHtml(field.detail)}</small>` : ""}
                                <input
                                    type="${escapeHtml(field?.type || "text")}"
                                    data-setup-field="${fieldKey}"
                                    autocomplete="off"
                                    ${field?.min ? `min="${escapeHtml(field.min)}"` : ""}
                                    ${field?.max ? `max="${escapeHtml(field.max)}"` : ""}
                                    placeholder="Enter ${escapeHtml(field?.label || field?.key || "required value")}"
                                >
                            </label>
                        `;
                    }).join("") : `
                        <textarea
                            rows="3"
                            data-setup-answer-input="${encodedKey}"
                            placeholder="Enter the information this employee needs"
                        ></textarea>
                    `}
                    <button
                        type="button"
                        class="employee-builder-journey-action"
                        data-save-setup-answer="${encodedKey}"
                    >Save setup data</button>
                    <div class="error" data-setup-answer-error="${encodedKey}"></div>
                </div>
            `;
        }

        const runnable = new Set([
            "choose_plan",
            "build_employee",
            "manage_knowledge",
            "manage_integrations",
            "setup_website",
            "setup_whatsapp",
            "setup_webhook",
            "test_employee",
            "launch_employee",
        ]).has(type);
        if (!runnable) return "";
        return `
            <button
                type="button"
                class="employee-builder-journey-action"
                data-builder-action="${escapeHtml(type)}"
            >${escapeHtml(action?.label || "Continue")}</button>
        `;
    }

    function journeyMarkup(employee) {
        if (employee.delivery_mode !== "self_service") return "";
        const journey = employee.builder_journey || {};
        const graphTrigger = employee.compiled_spec?.delivery?.graph_trigger || {};
        const manualGraphReady = Boolean(
            employee.enabled
            && graphTrigger.trigger_type === "manual"
            && graphTrigger.status === "ready"
            && graphTrigger.workflow_id
        );
        const stages = Array.isArray(journey.stages) ? journey.stages : [];
        if (!stages.length) return "";
        const progress = Number(journey.total_count || stages.length)
            ? Math.round((Number(journey.complete_count || 0) / Number(journey.total_count || stages.length)) * 100)
            : 0;

        return `
            <div class="panel employee-builder-journey">
                <div class="employee-builder-current-head">
                    <div>
                        <div class="employee-builder-kicker">BUILD PROGRESS</div>
                        <h2>From Job Brief to a live employee</h2>
                        <p class="muted">Xvond builds the employee. You only complete the plan, data or connections the job actually requires.</p>
                    </div>
                    ${badge(`${Number(journey.complete_count || 0)}/${Number(journey.total_count || stages.length)} ready`, progress === 100 ? "ready" : "neutral")}
                </div>
                <div class="employee-builder-progress" aria-label="Employee build progress">
                    <span style="width:${Math.max(0, Math.min(100, progress))}%"></span>
                </div>
                <div class="employee-builder-journey-list">
                    ${stages.map((stage, index) => `
                        <div class="employee-builder-journey-step employee-builder-journey-${escapeHtml(stage.status || "blocked")}">
                            <div class="employee-builder-journey-index">${index + 1}</div>
                            <div class="employee-builder-journey-copy">
                                <div class="employee-builder-current-head">
                                    <strong>${escapeHtml(stage.label || stage.id || "Step")}</strong>
                                    ${badge(journeyStatusLabel(stage.status), journeyTone(stage.status))}
                                </div>
                                <p class="muted">${escapeHtml(stage.detail || "")}</p>
                                ${(stage.actions || []).length ? `
                                    <div class="employee-builder-actions employee-builder-journey-actions">
                                        ${(stage.actions || []).map(journeyActionMarkup).join("")}
                                    </div>
                                ` : ""}
                            </div>
                        </div>
                    `).join("")}
                </div>
                <div id="subscription-plans" class="employee-builder-section hidden"></div>
                <div id="subscription-error" class="error"></div>
                <div id="prepare-employee-error" class="error"></div>
                ${manualGraphReady ? `
                    <div id="employee-builder-manual-run-panel" class="employee-builder-section">
                        <h3>Run now</h3>
                        <p class="muted">Run this employee's live execution graph now. Optional JSON becomes the graph input.</p>
                        <textarea id="employee-builder-manual-run-input" rows="4" placeholder='{"key":"value"}'></textarea>
                        <button type="button" id="employee-builder-manual-run">Run employee</button>
                        <pre id="employee-builder-manual-run-output" class="employee-builder-run-output hidden"></pre>
                        <div id="employee-builder-manual-run-error" class="error"></div>
                    </div>
                ` : ""}
                <div id="employee-builder-approvals-panel" class="employee-builder-section">
                    <div class="employee-builder-current-head">
                        <div>
                            <h3>Approvals</h3>
                            <p class="muted">Review consequential actions this employee is waiting to execute.</p>
                        </div>
                        <button type="button" id="employee-builder-approvals-refresh">Refresh</button>
                    </div>
                    <div id="employee-builder-approvals-list"><p class="muted">No approval data loaded yet.</p></div>
                    <div id="employee-builder-approvals-error" class="error"></div>
                </div>
                <div id="employee-builder-runs-panel" class="employee-builder-section">
                    <div class="employee-builder-current-head">
                        <div>
                            <h3>Execution history</h3>
                            <p class="muted">Inspect recent automatic runs, outputs and the exact failing node when something goes wrong.</p>
                        </div>
                        <button type="button" id="employee-builder-runs-refresh">Refresh</button>
                    </div>
                    <div id="employee-builder-runs-list"><p class="muted">Load recent runs to inspect execution.</p></div>
                    <div id="employee-builder-runs-error" class="error"></div>
                </div>
                <div id="employee-builder-webhook-panel" class="employee-builder-section hidden">
                    <h3>Webhook trigger</h3>
                    <p class="muted">Send JSON to this URL and include both headers below. Reuse a stable Idempotency-Key for retries of the same external event.</p>
                    <label><span>Webhook URL</span><input id="employee-builder-webhook-url" type="text" readonly></label>
                    <label><span>X-Xvond-Webhook-Key</span><input id="employee-builder-webhook-key" type="text" readonly></label>
                    <label><span>Idempotency header</span><input id="employee-builder-webhook-idempotency" type="text" readonly></label>
                    <div id="employee-builder-webhook-error" class="error"></div>
                </div>
                <div id="employee-builder-test-panel" class="employee-builder-section hidden">
                    <h3>Preview & Test</h3>
                    <p class="muted">${employee.pending_revision ? "Test the pending revision safely while the current employee stays live. No live channels or business actions are used in preview." : "Talk to the current draft. Xvond will not use live channels or execute business actions in this preview."}</p>
                    <div id="employee-builder-test-log" class="chat-box"></div>
                    <div class="chat-input">
                        <input id="employee-builder-test-message" maxlength="12000" placeholder="Try a real customer question...">
                        <button type="button" id="employee-builder-test-send">Send test</button>
                    </div>
                    <div id="employee-builder-test-error" class="error"></div>
                </div>
                <div id="launch-employee-error" class="error"></div>
            </div>
        `;
    }

    function selfServiceMarkup(employee) {
        if (employee.delivery_mode !== "self_service") return "";
        const state = employee.self_service_readiness || {};
        const subscription = state.subscription || {};
        const limit = state.channel_limit == null ? "—" : String(state.channel_limit);
        const used = Number(state.channel_slots_used || 0);
        const modeLabels = {
            personal: "Personal agent",
            background: "Background worker",
            customer_facing: "Customer-facing employee",
            hybrid: "Hybrid employee",
            workspace: "Workspace employee",
        };
        return `
            <div class="panel">
                <div class="employee-builder-kicker">SELF-SERVICE DELIVERY</div>
                <div class="employee-builder-summary-grid">
                    <div>
                        <h3>Work mode</h3>
                        <div class="employee-builder-missing">${badge(modeLabels[state.mode] || state.mode || "Pending")}</div>
                        <p class="muted">${state.channels_required ? "This job needs a communication channel." : "This job can run with 0 communication channels."}</p>
                    </div>
                    <div>
                        <h3>Channel slots</h3>
                        <div class="employee-builder-missing">${badge(`${used}/${limit}`, used > 0 ? "neutral" : "ready")}</div>
                        <p class="muted">Communication channels use plan slots. Gmail, email actions, Instagram publishing and other connected systems are integrations, not channel slots.</p>
                    </div>
                    <div>
                        <h3>Subscription</h3>
                        <div class="employee-builder-missing">${badge(subscription.active ? (subscription.plan_name || "Active") : "Subscription required", subscription.active ? "ready" : "setup")}</div>
                    </div>
                </div>
                <div class="employee-builder-section">
                    <h3>Runtime readiness</h3>
                    <div class="employee-builder-missing">
                        ${badge(
                            employee.enabled ? "Live" : (state.ready ? "Ready to launch" : "Follow build progress"),
                            employee.enabled || state.ready ? "ready" : "setup"
                        )}
                    </div>
                    <p class="muted">${state.ready || employee.enabled
                        ? "The runtime readiness gate is satisfied."
                        : "The Build Progress above is the customer-facing source for the next required step."}</p>
                </div>
            </div>
        `;
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
                    <div class="employee-builder-section" id="employee-builder-setup-questions">
                        <h3>Needed from you</h3>
                        <ul>${questions}</ul>
                    </div>
                ` : ""}
            </div>
        `;
    }

    function pendingRevisionMarkup(employee) {
        const pending = employee?.pending_revision;
        if (!pending) return "";

        const spec = pending.compiled_spec || null;
        const setupActions = [];
        for (const requirement of (spec?.requirements || [])) {
            if (!requirement || typeof requirement !== "object") continue;
            const key = String(requirement.key || "");
            const status = String(requirement.status || "");
            const kind = String(requirement.kind || "");
            if (status === "customer_input_required") {
                const fields = Array.isArray(requirement.customer_inputs)
                    ? requirement.customer_inputs.map(field => ({
                        key: String(field || ""),
                        label: String(field || "").replaceAll("_", " "),
                    })).filter(field => field.key)
                    : [];
                setupActions.push({
                    type: "provide_input",
                    label: "Provide " + key.replaceAll("_", " "),
                    key,
                    detail: requirement.purpose || "",
                    fields,
                });
            } else if (status === "connection_required" && kind !== "channel") {
                setupActions.push({
                    type: "connect_system",
                    label: "Connect system for " + key.replaceAll("_", " "),
                    key,
                    detail: requirement.purpose || "",
                });
            }
        }

        const currentChannels = new Set(employee.requested_channels || []);
        for (const channel of (pending.requested_channels || [])) {
            if (currentChannels.has(channel)) continue;
            if (channel === "website") {
                setupActions.push({type: "setup_website", label: "Set up Website Chat", key: "website"});
            } else if (channel === "whatsapp") {
                setupActions.push({type: "setup_whatsapp", label: "Set up WhatsApp", key: "whatsapp"});
            }
        }

        const statusBadge = badge(
            pending.current_build_tested ? "Tested" : (pending.compiled ? "Built" : "Draft"),
            pending.current_build_tested ? "ready" : "setup"
        );
        const channelBadges = (pending.requested_channels || []).map(item =>
            badge(labelFor(CHANNELS, item))
        ).join("") || '<span class="muted">No communication channel changes.</span>';
        const specMarkup = spec ? [
            '<div class="employee-builder-section">',
            "<h3>" + escapeHtml(spec.role || "AI Employee") + "</h3>",
            "<p>" + escapeHtml(spec.summary || "") + "</p>",
            requirementMarkup(spec.requirements),
            "</div>",
        ].join("") : '<div class="employee-builder-section"><p class="muted">This revision is saved but has not been built yet.</p></div>';
        const setupMarkup = setupActions.length ? [
            '<div class="employee-builder-section">',
            "<h3>Setup for this revision</h3>",
            '<p class="muted">These changes affect only the pending revision until Apply.</p>',
            '<div class="employee-builder-journey-actions">',
            setupActions.map(journeyActionMarkup).join(""),
            "</div></div>",
        ].join("") : "";
        const buildButton = pending.compiled
            ? ""
            : '<button type="button" id="pending-revision-build">Build revision</button>';
        const testButton = pending.compiled
            ? '<button type="button" id="pending-revision-test">' + (pending.current_build_tested ? "Test again" : "Test revision") + "</button>"
            : "";
        const applyButton = '<button type="button" id="pending-revision-apply" ' + (pending.can_apply ? "" : "disabled") + ">" + (pending.can_apply ? "Apply revision" : "Test before apply") + "</button>";

        return [
            '<div class="panel employee-builder-pending-revision">',
            '<div class="employee-builder-current-head"><div>',
            '<div class="employee-builder-kicker">PENDING REVISION</div>',
            "<h2>New version ready beside the live employee</h2>",
            '<p class="muted">The current live employee keeps running until you explicitly apply this revision.</p>',
            "</div>", statusBadge, "</div>",
            '<div class="employee-builder-section"><h3>Revised Job Brief</h3><p>',
            escapeHtml(pending.job_brief || ""),
            '</p><div class="employee-builder-missing">', channelBadges, "</div></div>",
            specMarkup,
            setupMarkup,
            '<div class="employee-builder-actions">',
            buildButton, testButton, applyButton,
            '<button type="button" id="pending-revision-discard">Discard revision</button>',
            "</div>",
            '<div id="pending-revision-error" class="error"></div>',
            "</div>",
        ].join("");
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
                        ${employee.delivery_mode === "self_service" && !employee.enabled ? `
                            <div class="employee-builder-actions">
                                <button type="button" id="revise-job-brief-btn">Revise job brief</button>
                            </div>
                            <div id="job-brief-editor" class="employee-builder-revision hidden">
                                <label for="job-brief-editor-input"><strong>Updated Job Brief</strong></label>
                                <textarea id="job-brief-editor-input" rows="8" maxlength="4000"></textarea>
                                <p class="muted">Saving a revision clears only Xvond-generated build artifacts from the old brief. Manual settings and connected external systems stay intact.</p>
                                <div class="employee-builder-actions">
                                    <button type="button" id="save-job-brief-btn">${employee.can_compile ? "Save & rebuild" : "Save job brief"}</button>
                                    <button type="button" id="cancel-job-brief-btn">Cancel</button>
                                </div>
                                <div id="job-brief-error" class="error"></div>
                            </div>
                        ` : ""}
                        ${employee.delivery_mode === "self_service" ? `
                            <div class="employee-builder-section">
                                <h3>Tell Xvond what to change</h3>
                                <p class="muted">Refine the same employee with a short instruction. Xvond keeps the rest of the Job Brief unless your new instruction overrides it.</p>
                                <div class="chat-input">
                                    <input id="employee-refine-instruction" maxlength="2000" placeholder="مثال: خليه يحكي رسمي أكثر، وخلي الحجز 30 دقيقة">
                                    <button type="button" id="employee-refine-btn">Apply change</button>
                                </div>
                                <div id="employee-refine-error" class="error"></div>
                            </div>
                            ${(employee.versions || []).length ? `
                                <details class="employee-builder-section">
                                    <summary><strong>Version history</strong> · ${Number((employee.versions || []).length)} saved</summary>
                                    <div style="margin-top:12px">
                                        ${(employee.versions || []).slice(0, 10).map(version => `
                                            <div class="note">
                                                <div class="employee-builder-current-head">
                                                    <div>
                                                        <strong>${escapeHtml(version.reason || "Previous build")}</strong>
                                                        <div class="muted">${escapeHtml(version.created_at || "")}</div>
                                                    </div>
                                                    <button type="button" data-rollback-version="${escapeHtml(version.id || "")}">${employee.enabled ? "Stage restore" : "Restore"}</button>
                                                </div>
                                                <p class="muted">${escapeHtml(String(version.job_brief || "").slice(0, 240))}</p>
                                            </div>
                                        `).join("")}
                                    </div>
                                </details>
                            ` : ""}
                        ` : ""}
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

                    ${employee.delivery_mode === "self_service" ? "" : (provisioned ? "" : employee.can_compile ? `
                        <div class="employee-builder-section">
                            <h3>Build this employee</h3>
                            <p class="muted">Xvond will understand the complete job, break it into tasks and identify the setup it needs.</p>
                            <div class="employee-builder-actions">
                                <button type="button" id="prepare-employee-btn">Build employee</button>
                            </div>
                            <div id="prepare-employee-error" class="error"></div>
                        </div>
                    ` : `
                        <div class="employee-builder-section">
                            <h3>Next step</h3>
                            <p class="muted">Activate the required AI Employee service before building.</p>
                        </div>
                    `)}

                    ${employee.delivery_mode === "self_service" ? "" : `
                        <div class="employee-builder-section">
                            <h3>Needed from you</h3>
                            ${missingMarkup(employee.missing_information)}
                        </div>
                    `}
                </div>

                ${journeyMarkup(employee)}
                ${compiledMarkup(employee.compiled_spec)}
                ${pendingRevisionMarkup(employee)}
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

        async function openJourneyPage(pageId) {
            const navButton = [...document.querySelectorAll("#portal-nav .nav-item")]
                .find(item => item.dataset.page === pageId);
            if (typeof window.openPage === "function") {
                await window.openPage(pageId, navButton || null);
            }
        }

        async function runJourneyAction(actionType) {
            if (actionType === "choose_plan") {
                await loadSubscriptionPlans();
                document.getElementById("subscription-plans")?.scrollIntoView({behavior: "smooth", block: "center"});
                return;
            }
            if (actionType === "build_employee") {
                await prepareEmployee(employee.agent_id);
                return;
            }
            if (actionType === "test_employee") {
                const panel = document.getElementById("employee-builder-test-panel");
                panel?.classList.remove("hidden");
                panel?.scrollIntoView({behavior: "smooth", block: "center"});
                document.getElementById("employee-builder-test-message")?.focus();
                return;
            }
            if (actionType === "launch_employee") {
                await launchEmployee(employee.agent_id);
                return;
            }
            if (actionType === "manage_knowledge") {
                await openJourneyPage("agents");
                if (typeof window.openCustomerAgentSettings === "function") {
                    await window.openCustomerAgentSettings(employee.agent_id);
                }
                if (typeof window.openCustomerManagerTab === "function") {
                    await window.openCustomerManagerTab("knowledge");
                }
                document.getElementById("customer-manager-tab")?.scrollIntoView({behavior: "smooth", block: "start"});
                return;
            }
            if (actionType === "manage_integrations") {
                await openJourneyPage("integrations");
                return;
            }
            if (actionType === "setup_webhook") {
                const panel = document.getElementById("employee-builder-webhook-panel");
                const error = document.getElementById("employee-builder-webhook-error");
                if (error) error.textContent = "";
                try {
                    const result = await api(`/customer/employee-builder/${Number(employee.agent_id)}/webhook`);
                    const url = document.getElementById("employee-builder-webhook-url");
                    const key = document.getElementById("employee-builder-webhook-key");
                    const idempotency = document.getElementById("employee-builder-webhook-idempotency");
                    if (url) url.value = String(result.url || "");
                    if (key) key.value = String(result.key || "");
                    if (idempotency) idempotency.value = String(result.idempotency_header || "Idempotency-Key");
                    panel?.classList.remove("hidden");
                    panel?.scrollIntoView({behavior: "smooth", block: "center"});
                } catch (err) {
                    panel?.classList.remove("hidden");
                    if (error) error.textContent = err?.message || "Could not load webhook setup.";
                }
                return;
            }
            if (actionType === "setup_website" || actionType === "setup_whatsapp") {
                await openJourneyPage("agents");
                const index = (agents || []).findIndex(item => Number(item.id) === Number(employee.agent_id));
                const card = index >= 0
                    ? Array.from(document.querySelectorAll("#agents-list .agent"))[index]
                    : null;
                const selector = actionType === "setup_website"
                    ? ".xvond-website-connect"
                    : ".xvond-whatsapp-connect";
                const setup = card?.querySelector(selector);
                setup?.scrollIntoView({behavior: "smooth", block: "center"});
                if (actionType === "setup_website") {
                    const toggle = setup?.querySelector(".xvond-website-toggle");
                    const form = setup?.querySelector(".xvond-website-form");
                    if (toggle && form?.classList.contains("hidden")) toggle.click();
                }
            }
        }

        document.querySelectorAll("[data-builder-action]").forEach(button => {
            button.addEventListener("click", async () => {
                if (button.disabled) return;
                button.disabled = true;
                try {
                    await runJourneyAction(String(button.dataset.builderAction || ""));
                } finally {
                    if (document.body.contains(button)) button.disabled = false;
                }
            });
        });

        async function loadExecutionHistory() {
            const list = document.getElementById("employee-builder-runs-list");
            const error = document.getElementById("employee-builder-runs-error");
            if (!list) return;
            if (error) error.textContent = "";
            list.innerHTML = '<p class="muted">Loading execution history...</p>';
            try {
                const result = await api(`/customer/employee-builder/${Number(employee.agent_id)}/automation-runs`);
                const runs = Array.isArray(result.runs) ? result.runs : [];
                if (!runs.length) {
                    list.innerHTML = '<p class="muted">No automatic runs yet.</p>';
                    return;
                }
                list.innerHTML = runs.map(run => {
                    const output = run.output_data ? JSON.stringify(run.output_data, null, 2) : "";
                    return `
                        <div class="note">
                            <div class="employee-builder-current-head">
                                <strong>${escapeHtml(run.workflow_name || `Run #${run.id}`)}</strong>
                                ${badge(String(run.status || "unknown"), run.status === "success" ? "ready" : (run.status === "failed" ? "setup" : "neutral"))}
                            </div>
                            <div class="muted">Run #${Number(run.id)} · ${escapeHtml(String(run.created_at || ""))}</div>
                            ${run.error_message ? `<div class="error">${escapeHtml(run.error_message)}</div>` : ""}
                            ${output ? `<pre class="employee-builder-run-output">${escapeHtml(output.slice(0, 6000))}</pre>` : ""}
                        </div>
                    `;
                }).join("");
            } catch (err) {
                list.innerHTML = "";
                if (error) error.textContent = err?.message || "Could not load execution history.";
            }
        }

        document.getElementById("employee-builder-runs-refresh")?.addEventListener("click", loadExecutionHistory);
        loadExecutionHistory();

        async function loadAutomationApprovals() {
            const list = document.getElementById("employee-builder-approvals-list");
            const error = document.getElementById("employee-builder-approvals-error");
            if (!list) return;
            if (error) error.textContent = "";
            list.innerHTML = '<p class="muted">Loading approvals...</p>';
            try {
                const result = await api(`/customer/employee-builder/${Number(employee.agent_id)}/automation-approvals`);
                const approvals = Array.isArray(result.approvals) ? result.approvals : [];
                if (!approvals.length) {
                    list.innerHTML = '<p class="muted">No actions are waiting for approval.</p>';
                    return;
                }
                list.innerHTML = approvals.map(item => `
                    <div class="note" data-automation-approval-row="${Number(item.id)}">
                        <div class="employee-builder-current-head">
                            <strong>${escapeHtml(item.summary || item.action_type || "Pending action")}</strong>
                            ${badge("Waiting approval", "setup")}
                        </div>
                        <div class="muted">
                            ${escapeHtml(String(item.action_type || ""))}
                            ${item.node_id ? ` · node ${escapeHtml(String(item.node_id))}` : ""}
                            ${item.run_id ? ` · run #${Number(item.run_id)}` : ""}
                        </div>
                        <pre class="employee-builder-run-output">${escapeHtml(JSON.stringify(item.details || {}, null, 2).slice(0, 4000))}</pre>
                        <div class="employee-builder-actions">
                            <button type="button" data-approve-automation="${Number(item.id)}">Approve & continue</button>
                            <button type="button" data-reject-automation="${Number(item.id)}">Reject</button>
                        </div>
                        <div class="error" data-approval-error="${Number(item.id)}"></div>
                    </div>
                `).join("");

                list.querySelectorAll("[data-approve-automation]").forEach(button => {
                    button.addEventListener("click", async () => {
                        const requestId = Number(button.dataset.approveAutomation || 0);
                        const rowError = list.querySelector(`[data-approval-error="${requestId}"]`);
                        if (rowError) rowError.textContent = "";
                        button.disabled = true;
                        try {
                            await api(
                                `/customer/employee-builder/${Number(employee.agent_id)}/automation-approvals/${requestId}/approve`,
                                {method: "POST"}
                            );
                            await Promise.all([
                                loadAutomationApprovals(),
                                loadExecutionHistory(),
                            ]);
                        } catch (err) {
                            if (rowError) rowError.textContent = err?.message || "Could not approve this action.";
                            button.disabled = false;
                        }
                    });
                });

                list.querySelectorAll("[data-reject-automation]").forEach(button => {
                    button.addEventListener("click", async () => {
                        const requestId = Number(button.dataset.rejectAutomation || 0);
                        const rowError = list.querySelector(`[data-approval-error="${requestId}"]`);
                        if (rowError) rowError.textContent = "";
                        button.disabled = true;
                        try {
                            await api(
                                `/customer/employee-builder/${Number(employee.agent_id)}/automation-approvals/${requestId}/reject`,
                                {method: "POST"}
                            );
                            await Promise.all([
                                loadAutomationApprovals(),
                                loadExecutionHistory(),
                            ]);
                        } catch (err) {
                            if (rowError) rowError.textContent = err?.message || "Could not reject this action.";
                            button.disabled = false;
                        }
                    });
                });
            } catch (err) {
                list.innerHTML = "";
                if (error) error.textContent = err?.message || "Could not load approvals.";
            }
        }

        document.getElementById("employee-builder-approvals-refresh")?.addEventListener("click", loadAutomationApprovals);
        loadAutomationApprovals();

        document.getElementById("employee-builder-manual-run")?.addEventListener("click", async () => {
            const input = document.getElementById("employee-builder-manual-run-input");
            const output = document.getElementById("employee-builder-manual-run-output");
            const error = document.getElementById("employee-builder-manual-run-error");
            if (error) error.textContent = "";
            let inputData = {};
            const raw = String(input?.value || "").trim();
            if (raw) {
                try {
                    inputData = JSON.parse(raw);
                    if (!inputData || Array.isArray(inputData) || typeof inputData !== "object") {
                        throw new Error("Input must be a JSON object.");
                    }
                } catch (err) {
                    if (error) error.textContent = err?.message || "Input must be valid JSON.";
                    return;
                }
            }
            try {
                const result = await api(
                    `/customer/employee-builder/${Number(employee.agent_id)}/run-graph`,
                    {
                        method: "POST",
                        body: JSON.stringify({input_data: inputData}),
                    }
                );
                if (output) {
                    output.textContent = JSON.stringify(result.output_data || result, null, 2);
                    output.classList.remove("hidden");
                }
                await loadExecutionHistory();
            } catch (err) {
                if (error) error.textContent = err?.message || "Could not run this employee.";
            }
        });

        async function loadBuilderConnections() {
            const selects = Array.from(document.querySelectorAll("[data-integration-select]"));
            if (!selects.length) return;
            try {
                const result = await api("/manage/integrations");
                const integrations = (result.integrations || []).filter(item => item.enabled && item.configured && item.validated);
                for (const select of selects) {
                    const requirementKey = decodeURIComponent(String(select.dataset.integrationSelect || ""));
                    const allowedTypes = requirementKey === "instagram_publish"
                        ? new Set(["instagram_publish"])
                        : (requirementKey === "email_send"
                            ? new Set(["email_smtp"])
                            : (requirementKey === "email_read"
                                ? new Set(["email_imap"])
                                : null));
                    const compatible = allowedTypes
                        ? integrations.filter(item => allowedTypes.has(String(item.integration_type || "")))
                        : integrations;
                    const options = '<option value="">Choose a connected system</option>' + compatible.map(item =>
                        `<option value="${Number(item.id)}" data-integration-type="${escapeHtml(item.integration_type)}">${escapeHtml(item.name)} · ${escapeHtml(item.integration_type)}</option>`
                    ).join("");
                    select.innerHTML = options;
                    const syncEndpointFields = () => {
                        const encodedKey = String(select.dataset.integrationSelect || "");
                        const wrapper = document.querySelector(`[data-integration-endpoint-fields="${encodedKey}"]`);
                        const selectedType = String(select.selectedOptions?.[0]?.dataset?.integrationType || "");
                        if (wrapper) wrapper.classList.toggle(
                            "hidden",
                            new Set(["instagram_publish", "email_smtp", "email_imap"]).has(selectedType)
                        );
                    };
                    select.addEventListener("change", syncEndpointFields);
                    syncEndpointFields();
                }
            } catch (err) {
                for (const select of selects) {
                    select.innerHTML = '<option value="">Could not load connections</option>';
                }
            }
        }
        loadBuilderConnections();

        document.querySelectorAll("[data-open-integrations]").forEach(button => {
            button.addEventListener("click", () => openJourneyPage("integrations"));
        });

        document.querySelectorAll("[data-bind-integration]").forEach(button => {
            button.addEventListener("click", async () => {
                const encodedKey = String(button.dataset.bindIntegration || "");
                const key = decodeURIComponent(encodedKey);
                const select = document.querySelector(`[data-integration-select="${encodedKey}"]`);
                const execute = document.querySelector(`[data-integration-execute="${encodedKey}"]`);
                const availability = document.querySelector(`[data-integration-availability="${encodedKey}"]`);
                const cancel = document.querySelector(`[data-integration-cancel="${encodedKey}"]`);
                const error = document.querySelector(`[data-integration-bind-error="${encodedKey}"]`);
                if (error) error.textContent = "";
                const integrationId = Number(select?.value || 0);
                if (!integrationId) {
                    if (error) error.textContent = "Choose a connected system first.";
                    return;
                }
                button.disabled = true;
                try {
                    await api(
                        `/customer/employee-builder/${Number(employee.agent_id)}/connections/${encodeURIComponent(key)}`,
                        {
                            method: "POST",
                            body: JSON.stringify({
                                integration_id: integrationId,
                                execute_endpoint: String(execute?.value || "").trim() || null,
                                availability_endpoint: String(availability?.value || "").trim() || null,
                                cancel_endpoint: String(cancel?.value || "").trim() || null,
                            }),
                        }
                    );
                    await loadEmployeeBuilder();
                } catch (err) {
                    if (error) error.textContent = err?.message || "Could not connect this system.";
                } finally {
                    if (document.body.contains(button)) button.disabled = false;
                }
            });
        });

        document.querySelectorAll("[data-save-setup-answer]").forEach(button => {
            button.addEventListener("click", async () => {
                if (button.disabled) return;
                const encodedKey = String(button.dataset.saveSetupAnswer || "");
                const key = decodeURIComponent(encodedKey);
                const wrapper = button.closest(".employee-builder-setup-answer");
                const input = wrapper?.querySelector(`[data-setup-answer-input="${encodedKey}"]`);
                const fieldInputs = Array.from(wrapper?.querySelectorAll("[data-setup-field]") || []);
                const error = wrapper?.querySelector(`[data-setup-answer-error="${encodedKey}"]`);
                if (error) error.textContent = "";

                const values = {};
                for (const fieldInput of fieldInputs) {
                    const fieldKey = decodeURIComponent(String(fieldInput.dataset.setupField || ""));
                    values[fieldKey] = String(fieldInput.value || "").trim();
                }
                const value = String(input?.value || "").trim();
                if (fieldInputs.length && Object.values(values).some(item => !item)) {
                    if (error) error.textContent = "Complete every required setup field.";
                    return;
                }
                if (!fieldInputs.length && !value) {
                    if (error) error.textContent = "Enter the required setup information.";
                    return;
                }

                button.disabled = true;
                try {
                    await api(
                        `/customer/employee-builder/${Number(employee.agent_id)}/setup/${encodeURIComponent(key)}`,
                        {
                            method: "PUT",
                            body: JSON.stringify(fieldInputs.length ? {values} : {value})
                        }
                    );
                    await loadEmployeeBuilder();
                } catch (err) {
                    if (error) error.textContent = err?.message || "Could not save setup information.";
                } finally {
                    if (document.body.contains(button)) button.disabled = false;
                }
            });
        });

        const reviseButton = document.getElementById("revise-job-brief-btn");
        const revisionEditor = document.getElementById("job-brief-editor");
        const revisionInput = document.getElementById("job-brief-editor-input");
        const cancelRevisionButton = document.getElementById("cancel-job-brief-btn");
        const saveRevisionButton = document.getElementById("save-job-brief-btn");
        if (reviseButton && revisionEditor && revisionInput) {
            reviseButton.addEventListener("click", () => {
                revisionInput.value = employee.description || "";
                revisionEditor.classList.remove("hidden");
                reviseButton.disabled = true;
                revisionInput.focus();
            });
        }
        if (cancelRevisionButton && revisionEditor && reviseButton) {
            cancelRevisionButton.addEventListener("click", () => {
                revisionEditor.classList.add("hidden");
                reviseButton.disabled = false;
            });
        }
        if (saveRevisionButton && revisionInput) {
            saveRevisionButton.addEventListener("click", () => {
                saveJobBrief(employee.agent_id, revisionInput.value, employee.can_compile);
            });
        }

        const testInput = document.getElementById("employee-builder-test-message");
        const testSend = document.getElementById("employee-builder-test-send");
        if (testSend && testInput) {
            testSend.addEventListener("click", () => testEmployee(employee.agent_id));
            testInput.addEventListener("keydown", event => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    testEmployee(employee.agent_id);
                }
            });
        }

        const refineButton = document.getElementById("employee-refine-btn");
        const refineInput = document.getElementById("employee-refine-instruction");
        if (refineButton && refineInput) {
            refineButton.addEventListener("click", () => refineEmployee(employee.agent_id));
            refineInput.addEventListener("keydown", event => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    refineEmployee(employee.agent_id);
                }
            });
        }

        document.getElementById("pending-revision-build")?.addEventListener("click", async event => {
            const button = event.currentTarget;
            const error = document.getElementById("pending-revision-error");
            if (error) error.textContent = "";
            button.disabled = true;
            try {
                await api("/customer/employee-builder/" + Number(employee.agent_id) + "/build-pending-revision", {method: "POST"});
                await loadEmployeeBuilder();
            } catch (err) {
                if (error) error.textContent = err?.message || "Could not build this revision.";
                if (document.body.contains(button)) button.disabled = false;
            }
        });

        document.getElementById("pending-revision-test")?.addEventListener("click", () => {
            const panel = document.getElementById("employee-builder-test-panel");
            panel?.classList.remove("hidden");
            panel?.scrollIntoView({behavior: "smooth", block: "center"});
            document.getElementById("employee-builder-test-message")?.focus();
        });

        document.getElementById("pending-revision-apply")?.addEventListener("click", async event => {
            const button = event.currentTarget;
            const error = document.getElementById("pending-revision-error");
            if (error) error.textContent = "";
            button.disabled = true;
            try {
                await api("/customer/employee-builder/" + Number(employee.agent_id) + "/apply-pending-revision", {method: "POST"});
                await loadEmployeeBuilder();
            } catch (err) {
                if (error) error.textContent = err?.message || "Could not apply this revision.";
                if (document.body.contains(button)) button.disabled = false;
            }
        });

        document.getElementById("pending-revision-discard")?.addEventListener("click", async event => {
            if (!confirm("Discard this pending revision? The live employee will not change.")) return;
            const button = event.currentTarget;
            const error = document.getElementById("pending-revision-error");
            if (error) error.textContent = "";
            button.disabled = true;
            try {
                await api("/customer/employee-builder/" + Number(employee.agent_id) + "/discard-pending-revision", {method: "POST"});
                await loadEmployeeBuilder();
            } catch (err) {
                if (error) error.textContent = err?.message || "Could not discard this revision.";
                if (document.body.contains(button)) button.disabled = false;
            }
        });

        document.querySelectorAll("[data-rollback-version]").forEach(button => {
            button.addEventListener("click", async () => {
                const versionId = String(button.dataset.rollbackVersion || "");
                const message = employee.enabled ? "Stage this previous version as a pending revision? The live employee will keep running." : "Restore this employee version? The current draft will be saved in history first.";
                if (!versionId || !confirm(message)) return;
                button.disabled = true;
                try {
                    await api(`/customer/employee-builder/${employee.agent_id}/rollback`, {
                        method: "POST",
                        body: JSON.stringify({version_id: versionId}),
                    });
                    await loadEmployeeBuilder();
                } catch (err) {
                    alert(err?.message || "Could not restore this version.");
                } finally {
                    if (document.body.contains(button)) button.disabled = false;
                }
            });
        });

        const chooseSubscriptionButton = document.getElementById("choose-subscription-btn");
        if (chooseSubscriptionButton) {
            chooseSubscriptionButton.addEventListener("click", loadSubscriptionPlans);
        }

        const prepareButton = document.getElementById("prepare-employee-btn");
        if (prepareButton) {
            prepareButton.addEventListener("click", () => prepareEmployee(employee.agent_id));
        }
        const launchButton = document.getElementById("launch-employee-btn");
        if (launchButton) {
            launchButton.addEventListener("click", () => launchEmployee(employee.agent_id));
        }
    }

    async function refineEmployee(agentId) {
        const input = document.getElementById("employee-refine-instruction");
        const button = document.getElementById("employee-refine-btn");
        const error = document.getElementById("employee-refine-error");
        const instruction = String(input?.value || "").trim();
        if (error) error.textContent = "";
        if (instruction.length < 2) {
            if (error) error.textContent = "Tell Xvond what you want to change.";
            return;
        }
        if (button) button.disabled = true;
        try {
            await api(`/customer/employee-builder/${agentId}/refine`, {
                method: "POST",
                body: JSON.stringify({instruction}),
            });
            await loadEmployeeBuilder();
        } catch (err) {
            if (error) error.textContent = err?.message || "Could not apply this change.";
        } finally {
            if (button && document.body.contains(button)) button.disabled = false;
        }
    }

    async function saveJobBrief(agentId, description, rebuild) {
        const button = document.getElementById("save-job-brief-btn");
        const error = document.getElementById("job-brief-error");
        const clean = String(description || "").trim();
        if (error) error.textContent = "";
        if (clean.length < 8) {
            if (error) error.textContent = "Describe the employee job in a little more detail.";
            return;
        }
        if (button) button.disabled = true;
        let saved = false;
        try {
            await api(`/customer/employee-builder/${agentId}/job-brief`, {
                method: "PATCH",
                body: JSON.stringify({ description: clean })
            });
            saved = true;
            if (rebuild) {
                await api(`/customer/employee-builder/${agentId}/compile`, {
                    method: "POST",
                    body: "{}"
                });
            }
            await loadEmployeeBuilder();
        } catch (err) {
            if (error) {
                error.textContent = saved && rebuild
                    ? `Job Brief saved, but rebuild failed: ${err?.message || "Could not rebuild employee."}`
                    : (err?.message || "Could not update Job Brief.");
            }
        } finally {
            if (button) button.disabled = false;
        }
    }

    function subscriptionPlanMarkup(plan) {
        const price = Number(plan?.monthly_price || 0);
        const amount = Number.isFinite(price) ? price.toFixed(price % 1 ? 3 : 0) : String(plan?.monthly_price || 0);
        const limits = Object.entries(plan?.limits || {}).map(([key, value]) => `${escapeHtml(key.replaceAll("_", " "))}: ${String(value) === "0" ? "Unlimited" : escapeHtml(value)}`).join(" · ");
        return `
            <div class="agent">
                <h3>${escapeHtml(plan?.name || plan?.tier || "AI Employee")}</h3>
                <p><strong>${escapeHtml(plan?.currency || "OMR")} ${escapeHtml(amount)} / month</strong></p>
                <p class="muted">${limits || "Package limits are managed by Xvond."}</p>
                <button type="button" class="request-subscription-plan" data-plan-id="${Number(plan.id)}">${plan?.free ? "Activate free plan" : "Select plan"}</button>
            </div>
        `;
    }

    async function loadSubscriptionPlans() {
        const button = document.getElementById("choose-subscription-btn");
        const box = document.getElementById("subscription-plans");
        const error = document.getElementById("subscription-error");
        if (!box) return;
        if (button) button.disabled = true;
        if (error) error.textContent = "";
        try {
            const result = await api("/customer/subscription/ai-agents/plans");
            const plans = result.plans || [];
            box.classList.remove("hidden");
            box.innerHTML = plans.length
                ? `<div class="service-grid">${plans.map(subscriptionPlanMarkup).join("")}</div>${result.online_payments_enabled ? "" : '<p class="muted">Online payment is not enabled yet. Paid plans remain pending until payment or Xvond approval is completed.</p>'}`
                : '<p class="muted">No AI Employee plans are available yet.</p>';
            box.querySelectorAll(".request-subscription-plan").forEach(planButton => {
                planButton.addEventListener("click", () => requestSubscriptionPlan(Number(planButton.dataset.planId), planButton));
            });
        } catch (err) {
            if (error) error.textContent = err?.message || "Could not load subscription plans.";
        } finally {
            if (button) button.disabled = false;
        }
    }

    async function requestSubscriptionPlan(planId, button) {
        const error = document.getElementById("subscription-error");
        if (button) button.disabled = true;
        if (error) error.textContent = "";
        try {
            const result = await api("/customer/subscription/ai-agents/request", {
                method: "POST",
                body: JSON.stringify({ plan_id: Number(planId) })
            });
            if (result.requires_payment && result?.checkout?.checkout_url) {
                window.location.assign(result.checkout.checkout_url);
                return;
            }
            if (result.requires_payment) {
                alert("Plan selected. Payment or Xvond approval is required before AI execution is enabled.");
            }
            await loadEmployeeBuilder();
        } catch (err) {
            if (error) error.textContent = err?.message || "Could not select subscription plan.";
        } finally {
            if (button) button.disabled = false;
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

    function appendTestMessage(role, message) {
        const log = document.getElementById("employee-builder-test-log");
        if (!log) return;
        log.innerHTML += `<div class="chat-row"><strong>${escapeHtml(role)}</strong><div>${escapeHtml(message)}</div></div>`;
        log.scrollTop = log.scrollHeight;
    }

    async function testEmployee(agentId) {
        const input = document.getElementById("employee-builder-test-message");
        const button = document.getElementById("employee-builder-test-send");
        const error = document.getElementById("employee-builder-test-error");
        const message = String(input?.value || "").trim();
        if (!message) return;
        if (error) error.textContent = "";
        appendTestMessage("You", message);
        if (input) input.value = "";
        if (button) button.disabled = true;
        try {
            const result = await api(`/customer/employee-builder/${agentId}/test`, {
                method: "POST",
                body: JSON.stringify({message})
            });
            appendTestMessage("AI Employee", result.message || "");
            await loadEmployeeBuilder();
            requestAnimationFrame(() => {
                const panel = document.getElementById("employee-builder-test-panel");
                panel?.classList.remove("hidden");
            });
        } catch (err) {
            if (error) error.textContent = err?.message || "Could not test employee.";
        } finally {
            if (button && document.body.contains(button)) button.disabled = false;
        }
    }

    async function launchEmployee(agentId) {
        const button = document.getElementById("launch-employee-btn");
        const error = document.getElementById("launch-employee-error");
        if (button) button.disabled = true;
        if (error) error.textContent = "";
        try {
            await api(`/customer/employee-builder/${agentId}/launch`, {
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
