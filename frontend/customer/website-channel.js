function xvondCustomerWebsiteStatus(config) {
    if (config?.enabled) {
        return {
            title: "Website Chat Live",
            detail: "The website widget is active and uses this AI employee.",
        };
    }
    if (config?.prepared) {
        return {
            title: "Website Chat prepared",
            detail: "The channel is configured and waiting for launch.",
        };
    }
    return {
        title: "Set up Website Chat",
        detail: "Configure the website domain, widget experience and channel-specific human assistance.",
    };
}

function xvondWebsiteAssignedToAgent(agentId) {
    return (portalOverview?.channels || []).some(item =>
        Number(item.agent_id) === Number(agentId) &&
        String(item.type || "").toLowerCase() === "website"
    );
}

function xvondWebsiteSelfServiceSelected(agent) {
    const slots = Array.isArray(agent?.self_service_channel_slots)
        ? agent.self_service_channel_slots
        : [];
    return slots.includes("website");
}

function xvondWebsiteCanOpen(agent) {
    return xvondWebsiteAssignedToAgent(agent?.id) || xvondWebsiteSelfServiceSelected(agent);
}

function xvondWebsiteFieldValue(config, key, fallback = "") {
    const value = config?.config?.[key];
    return value === undefined || value === null ? fallback : String(value);
}

function xvondWebsiteModalMarkup(agent, config) {
    const status = xvondCustomerWebsiteStatus(config);
    const mode = xvondWebsiteFieldValue(config, "human_assistance_mode", "direct_handoff");
    const position = xvondWebsiteFieldValue(config, "position", "right");
    const editable = config?.can_edit !== false;
    return `
        <div class="xvond-channel-modal-backdrop" style="position:fixed;inset:0;z-index:10000;background:rgba(15,23,42,.66);display:flex;align-items:flex-start;justify-content:center;padding:28px 16px;overflow:auto">
            <div class="panel" style="width:min(760px,100%);margin:auto;max-height:none">
                <div class="service-card-head">
                    <div>
                        <h2 style="margin:0">Website Chat · ${safe(agent?.name || "AI Employee")}</h2>
                        <p class="muted" style="margin:6px 0 0">${safe(status.detail)}</p>
                    </div>
                    <button type="button" data-xvond-website-close>Close</button>
                </div>

                <div class="cards" style="margin:16px 0">
                    <div class="card"><span>Status</span><strong>${safe(status.title)}</strong></div>
                    <div class="card"><span>Domain</span><strong>${safe(xvondWebsiteFieldValue(config, "allowed_domain", "Not configured"))}</strong></div>
                    <div class="card"><span>Human assistance</span><strong>${safe(mode.replaceAll("_", " "))}</strong></div>
                </div>

                ${!editable ? '<div class="agent" style="margin-bottom:14px"><strong>Editing is locked while this AI Employee is active.</strong><p class="muted">Deactivate the employee before changing Website Chat settings. Existing configuration remains unchanged.</p></div>' : ""}

                <form data-xvond-website-form style="display:grid;gap:12px">
                    <div>
                        <label>Website domain</label>
                        <input data-field="allowed_domain" placeholder="example.com" required value="${safe(xvondWebsiteFieldValue(config, "allowed_domain"))}" ${editable ? "" : "disabled"}>
                    </div>
                    <div>
                        <label>Widget name</label>
                        <input data-field="widget_name" placeholder="Customer Assistant" value="${safe(xvondWebsiteFieldValue(config, "widget_name"))}" ${editable ? "" : "disabled"}>
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
                        <div>
                            <label>Launcher label · Arabic</label>
                            <input data-field="launcher_label_ar" placeholder="ابدأ المحادثة" value="${safe(xvondWebsiteFieldValue(config, "launcher_label_ar"))}" ${editable ? "" : "disabled"}>
                        </div>
                        <div>
                            <label>Launcher label · English</label>
                            <input data-field="launcher_label_en" placeholder="Chat" value="${safe(xvondWebsiteFieldValue(config, "launcher_label_en"))}" ${editable ? "" : "disabled"}>
                        </div>
                    </div>
                    <div>
                        <label>Welcome message · Arabic</label>
                        <textarea data-field="welcome_message" rows="2" ${editable ? "" : "disabled"}>${safe(xvondWebsiteFieldValue(config, "welcome_message"))}</textarea>
                    </div>
                    <div>
                        <label>Welcome message · English</label>
                        <textarea data-field="welcome_message_en" rows="2" ${editable ? "" : "disabled"}>${safe(xvondWebsiteFieldValue(config, "welcome_message_en"))}</textarea>
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
                        <div>
                            <label>Widget position</label>
                            <select data-field="position" ${editable ? "" : "disabled"}>
                                <option value="right" ${position === "right" ? "selected" : ""}>Right</option>
                                <option value="left" ${position === "left" ? "selected" : ""}>Left</option>
                            </select>
                        </div>
                        <div>
                            <label>Accent color</label>
                            <input data-field="accent_color" type="text" placeholder="#111827" value="${safe(xvondWebsiteFieldValue(config, "accent_color", "#111827"))}" ${editable ? "" : "disabled"}>
                        </div>
                    </div>

                    <div class="agent">
                        <strong>Human assistance on Website Chat</strong>
                        <p class="muted">This controls only the website channel. It does not change WhatsApp, Instagram or other channel behavior.</p>
                        <select data-field="human_assistance_mode" ${editable ? "" : "disabled"}>
                            <option value="direct_handoff" ${mode === "direct_handoff" ? "selected" : ""}>Direct handoff to a human</option>
                            <option value="contact_only" ${mode === "contact_only" ? "selected" : ""}>Show contact methods only</option>
                            <option value="ai_only" ${mode === "ai_only" ? "selected" : ""}>AI only · no human transfer</option>
                        </select>
                        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-top:10px">
                            <input data-field="contact_phone" placeholder="Phone" value="${safe(xvondWebsiteFieldValue(config, "contact_phone"))}" ${editable ? "" : "disabled"}>
                            <input data-field="contact_whatsapp" placeholder="WhatsApp" value="${safe(xvondWebsiteFieldValue(config, "contact_whatsapp"))}" ${editable ? "" : "disabled"}>
                            <input data-field="contact_email" type="email" placeholder="Email" value="${safe(xvondWebsiteFieldValue(config, "contact_email"))}" ${editable ? "" : "disabled"}>
                            <input data-field="contact_url" placeholder="Contact / booking URL" value="${safe(xvondWebsiteFieldValue(config, "contact_url"))}" ${editable ? "" : "disabled"}>
                        </div>
                    </div>

                    <div>
                        <label>Website-only instructions</label>
                        <textarea data-field="custom_instructions" rows="4" placeholder="Example: On the website, keep replies short and never offer human transfer after 10 PM." ${editable ? "" : "disabled"}>${safe(xvondWebsiteFieldValue(config, "custom_instructions"))}</textarea>
                        <p class="muted">The AI Employee's core knowledge and identity remain shared across channels. These instructions adapt behavior only for Website Chat.</p>
                    </div>

                    ${config?.embed_code ? `
                        <div>
                            <label>Embed code</label>
                            <textarea data-xvond-website-embed readonly rows="2"></textarea>
                            <button type="button" data-xvond-website-copy style="margin-top:6px">Copy embed code</button>
                        </div>
                    ` : ""}

                    <div class="error" data-xvond-website-error></div>
                    ${editable ? '<button type="submit">Save Website Chat settings</button>' : ""}
                </form>
            </div>
        </div>
    `;
}

function xvondWebsitePayload(form) {
    const value = key => form.querySelector(`[data-field="${key}"]`)?.value?.trim() || "";
    return {
        allowed_domain: value("allowed_domain"),
        widget_name: value("widget_name") || null,
        welcome_message: value("welcome_message") || null,
        welcome_message_en: value("welcome_message_en") || null,
        position: value("position") || "right",
        custom_instructions: value("custom_instructions") || null,
        accent_color: value("accent_color") || "#111827",
        launcher_label_ar: value("launcher_label_ar") || null,
        launcher_label_en: value("launcher_label_en") || null,
        human_assistance_mode: value("human_assistance_mode") || "direct_handoff",
        contact_phone: value("contact_phone") || null,
        contact_whatsapp: value("contact_whatsapp") || null,
        contact_email: value("contact_email") || null,
        contact_url: value("contact_url") || null,
    };
}

window.openCustomerWebsiteChannelSettings = async function(agentId) {
    const agent = (agents || []).find(item => Number(item.id) === Number(agentId));
    if (!agent) {
        alert("AI Employee was not found.");
        return;
    }
    try {
        const config = await api(`/customer/website-channel/agents/${Number(agentId)}`);
        const host = document.createElement("div");
        host.innerHTML = xvondWebsiteModalMarkup(agent, config);
        const overlay = host.firstElementChild;
        document.body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.querySelector("[data-xvond-website-close]")?.addEventListener("click", close);
        overlay.addEventListener("click", event => {
            if (event.target === overlay) close();
        });

        const embed = overlay.querySelector("[data-xvond-website-embed]");
        if (embed) embed.value = config.embed_code || "";
        overlay.querySelector("[data-xvond-website-copy]")?.addEventListener("click", async event => {
            try {
                await navigator.clipboard.writeText(embed?.value || "");
                event.currentTarget.textContent = "Copied";
            } catch (_) {
                embed?.select();
            }
        });

        const form = overlay.querySelector("[data-xvond-website-form]");
        form?.addEventListener("submit", async event => {
            event.preventDefault();
            const submit = form.querySelector('button[type="submit"]');
            const error = form.querySelector("[data-xvond-website-error]");
            if (submit) submit.disabled = true;
            if (error) error.textContent = "";
            try {
                await api(`/customer/website-channel/agents/${Number(agentId)}`, {
                    method: "PUT",
                    body: JSON.stringify(xvondWebsitePayload(form)),
                });
                if (typeof xvondRefreshCustomerOverview === "function") await xvondRefreshCustomerOverview();
                await loadAgents();
                close();
                if (typeof renderXvondChannelCenter === "function") await renderXvondChannelCenter();
            } catch (err) {
                if (error) error.textContent = err?.message || "Could not save Website Chat settings.";
            } finally {
                if (submit) submit.disabled = false;
            }
        });
    } catch (error) {
        alert(error.message || "Website Chat settings are unavailable.");
    }
};

async function xvondDecorateCustomerAgentsWithWebsite() {
    if (!currentUser || !["owner", "admin", "manager"].includes(currentUser.role)) return;

    const cards = Array.from(document.querySelectorAll("#agents-list .agent"));
    (agents || []).forEach((agent, index) => {
        if (!xvondWebsiteCanOpen(agent)) return;
        const card = cards[index];
        if (!card || card.querySelector(".xvond-website-connect")) return;

        const box = document.createElement("div");
        box.className = "xvond-website-connect";
        box.style.cssText = "margin-top:14px;padding-top:12px;border-top:1px solid rgba(148,163,184,.25)";
        box.innerHTML = `
            <strong>Website Chat</strong>
            <p class="muted" style="margin:6px 0 10px">Manage website-specific appearance, welcome messages, instructions and human assistance.</p>
            <button type="button" onclick="openCustomerWebsiteChannelSettings(${Number(agent.id)})">Manage Website Chat</button>
        `;
        card.appendChild(box);
    });
}

