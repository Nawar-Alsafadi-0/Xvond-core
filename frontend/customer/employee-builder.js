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
                                <input
                                    type="text"
                                    data-setup-field="${fieldKey}"
                                    autocomplete="off"
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
            "setup_website",
            "setup_whatsapp",
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
