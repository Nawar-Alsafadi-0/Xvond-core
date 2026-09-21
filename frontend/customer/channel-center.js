function xvondChannelCenterState(channel) {
    const type = String(channel?.type || "").toLowerCase();
    const provisioning = String(channel?.provisioning_state || "").toLowerCase();
    if (channel?.enabled) return {label: "Live", tone: "good", detail: "Serving customer traffic"};
    if (provisioning === "connected") return {label: "Connected", tone: "ready", detail: "Connected and waiting for launch verification"};
    if (provisioning === "requested") return {label: "Setup required", tone: "warn", detail: "Connection has not been completed"};
    if (provisioning === "cancelled") return {label: "Disconnected", tone: "muted", detail: "Connection was removed"};
    if (type === "website" && channel?.delivery_status === "ready_for_launch") {
        return {label: "Prepared", tone: "ready", detail: "Website setup is ready for launch"};
    }
    return {label: "Not connected", tone: "muted", detail: "Connect this channel to continue"};
}

function xvondChannelCenterAction(agent, channel) {
    const type = String(channel?.type || "").toLowerCase();
    const connected = String(channel?.provisioning_state || "").toLowerCase() === "connected" || channel?.enabled;
    if (type === "whatsapp") {
        return `<button type="button" onclick="openCustomerMetaWhatsAppConnect(${Number(agent.id)})">${connected ? "Reconnect / Change number" : "Connect with Meta"}</button>`;
    }
    if (type === "instagram" || type === "messenger") {
        const connectLabel = type === "instagram" ? "Connect Instagram" : "Connect with Meta";
        return `<button type="button" onclick="openCustomerMetaChannelConnect(${Number(agent.id)},'${type}')">${connected ? "Reconnect / Change account" : connectLabel}</button>`;
    }
    if (type === "website") {
        return `<button type="button" onclick="openCustomerWebsiteChannelSettings(${Number(agent.id)})">Manage Website Chat</button>`;
    }
    return `<button type="button" disabled>Managed by Xvond</button>`;
}

function xvondChannelCenterCard(agent, channel) {
    const state = xvondChannelCenterState(channel);
    const type = String(channel?.type || "").toLowerCase();
    const account = channel?.config?.provider_account_label || channel?.config?.display_phone_number || channel?.config?.allowed_domain || "";
    const color = state.tone === "good" ? "#16a34a" : state.tone === "ready" ? "#2563eb" : state.tone === "warn" ? "#d97706" : "#94a3b8";
    return `
        <div class="agent" style="display:grid;gap:10px">
            <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
                <div>
                    <strong>${safe(channel.name || xvondCustomerChannelLabel(type))}</strong>
                    <p class="muted" style="margin:5px 0 0">${safe(state.detail)}</p>
                </div>
                <span style="display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:700;white-space:nowrap">
                    <span style="width:8px;height:8px;border-radius:50%;background:${color}"></span>
                    ${safe(state.label)}
                </span>
            </div>
            ${account ? `<div class="muted">Connected account: <strong>${safe(account)}</strong></div>` : ""}
            <div style="display:flex;gap:8px;flex-wrap:wrap">
                ${xvondChannelCenterAction(agent, channel)}
                ${type === "whatsapp" ? `<button type="button" onclick="openCustomerWhatsAppChannelSettings(${Number(agent.id)})">Channel settings</button>` : ""}
                ${["instagram","messenger"].includes(type) ? `<button type="button" onclick="openCustomerMetaChannelSettings(${Number(agent.id)},'${type}')">Channel settings</button>` : ""}
                ${["telegram","email","sms","slack","teams","custom"].includes(type) ? `<button type="button" onclick="openCustomerGenericChannelSettings(${Number(agent.id)},'${type}')">Channel settings</button>` : ""}
                ${["whatsapp","instagram","messenger"].includes(type) && (String(channel?.provisioning_state||"").toLowerCase()==="connected" || channel?.enabled || type==="whatsapp") ? `<button type="button" onclick="xvondTestChannelConnection(${Number(agent.id)},'${type}')">Test connection</button>` : ""}
                ${["whatsapp","instagram","messenger"].includes(type) && (String(channel?.provisioning_state||"").toLowerCase()==="connected" || channel?.enabled || (type==="whatsapp" && channel?.config?.phone_number_id)) ? `<button type="button" onclick="xvondDisconnectCustomerChannel(${Number(agent.id)},'${type}')">Disconnect</button>` : ""}
                <button type="button" onclick="xvondRefreshChannelCenter()">Refresh status</button>
            </div>
        </div>
    `;
}

window.renderXvondChannelCenter = async function () {
    const page = document.getElementById("page-channels");
    const target = page?.querySelector(".dynamic-page-content");
    if (!target) return;
    target.innerHTML = '<div class="panel"><p class="muted">Loading channel connections...</p></div>';
    try {
        portalOverview = await api("/customer/overview");
        await loadAgents();
        const channels = portalOverview?.channels || [];
        target.innerHTML = `
            <div class="panel" style="margin-bottom:20px">
                <h2>Channel Connections</h2>
                <p class="muted">Connect customer-facing channels to each AI Employee. Account authorization is separate from final Xvond launch verification.</p>
            </div>
            ${(agents || []).map(agent => {
                const assigned = channels.filter(item => Number(item.agent_id) === Number(agent.id));
                return `
                    <div class="panel" style="margin-bottom:20px">
                        <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:14px">
                            <div>
                                <h3 style="margin:0">${safe(agent.name)}</h3>
                                <p class="muted" style="margin:5px 0 0">${assigned.length} assigned channel${assigned.length === 1 ? "" : "s"}</p>
                            </div>
                        </div>
                        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px">
                            ${assigned.length ? assigned.map(channel => xvondChannelCenterCard(agent, channel)).join("") : '<p class="muted">No channels are assigned to this AI Employee yet. Xvond Admin can assign channels during delivery setup.</p>'}
                        </div>
                    </div>
                `;
            }).join("") || '<div class="panel"><p>No AI Employees available.</p></div>'}
        `;
    } catch (error) {
        target.innerHTML = `<div class="panel"><p class="error">${safe(error.message || "Could not load channel connections.")}</p></div>`;
    }
};

window.xvondRefreshChannelCenter = async function () {
    await renderXvondChannelCenter();
};


window.xvondTestChannelConnection = async function(agentId, channelType) {
    const type = String(channelType || "").toLowerCase();
    try {
        if (type === "whatsapp") {
            const result = await api(`/customer/meta/whatsapp/embedded-signup/config?agent_id=${Number(agentId)}`);
            if (result.connected) {
                alert(result.runtime_ready
                    ? "WhatsApp connection is healthy and ready."
                    : `WhatsApp is connected. Remaining launch checks:\n${(result.blockers || []).join("\n") || "Xvond launch verification"}`);
            } else {
                alert(`WhatsApp connection needs attention.\n${result.connection_issue || "Reconnect the account."}`);
            }
            return;
        }
        if (type === "instagram" || type === "messenger") {
            const result = await api("/customer/meta/channels/health", {
                method: "POST",
                body: JSON.stringify({agent_id: Number(agentId), channel_type: type}),
            });
            if (result.healthy) {
                alert(`${xvondCustomerChannelLabel(type)} connection is healthy.`);
            } else {
                alert(`${xvondCustomerChannelLabel(type)} needs attention.\n${result.issue || "Reconnect the account."}`);
            }
            return;
        }
        alert("Connection testing for this channel is managed by Xvond.");
    } catch (error) {
        alert(error.message || "Could not verify the channel connection.");
    }
};

window.xvondDisconnectCustomerChannel = async function(agentId, channelType) {
    const type = String(channelType || "").toLowerCase();
    const name = xvondCustomerChannelLabel(type);
    if (!confirm(`Disconnect ${name} from this AI Employee?\n\nThe AI Employee and its conversation history stay in Xvond. You can reconnect the channel later.`)) return;
    try {
        if (type === "whatsapp") {
            await api("/customer/meta/whatsapp/disconnect", {
                method: "POST",
                body: JSON.stringify({agent_id: Number(agentId)}),
            });
        } else if (type === "instagram" || type === "messenger") {
            await api("/customer/meta/channels/disconnect", {
                method: "POST",
                body: JSON.stringify({agent_id: Number(agentId), channel_type: type}),
            });
        } else {
            alert("This channel is managed by Xvond and cannot be disconnected here.");
            return;
        }
        portalOverview = await api("/customer/overview");
        await renderXvondChannelCenter();
    } catch (error) {
        alert(error.message || "Could not disconnect the channel.");
    }
};


window.openCustomerGenericChannelSettings = async function(agentId, channelType) {
    const type = String(channelType || "").toLowerCase();
    const agent = (agents || []).find(item => Number(item.id) === Number(agentId));
    try {
        const result = await api(`/customer/agents/${Number(agentId)}/channels/${encodeURIComponent(type)}/settings`);
        const settings = result.settings || {};
        const overlay = document.createElement("div");
        overlay.style.cssText = "position:fixed;inset:0;z-index:10000;background:rgba(15,23,42,.66);display:flex;align-items:flex-start;justify-content:center;padding:28px 16px;overflow:auto";
        overlay.innerHTML = `
            <div class="panel" style="width:min(680px,100%);margin:auto">
                <div class="service-card-head">
                    <div>
                        <h2 style="margin:0">${safe(xvondCustomerChannelLabel(type))} Settings · ${safe(agent?.name || "AI Employee")}</h2>
                        <p class="muted" style="margin:6px 0 0">Behavior here applies only to this communication channel. Provider credentials stay managed by Xvond.</p>
                    </div>
                    <button type="button" data-xvond-generic-settings-close>Close</button>
                </div>
                <form data-xvond-generic-settings-form style="display:grid;gap:12px;margin-top:16px">
                    <div>
                        <label>Tone</label>
                        <select data-field="tone">
                            <option value="professional_friendly" ${settings.tone === "professional_friendly" ? "selected" : ""}>Professional & friendly</option>
                            <option value="formal" ${settings.tone === "formal" ? "selected" : ""}>Formal</option>
                            <option value="warm" ${settings.tone === "warm" ? "selected" : ""}>Warm</option>
                            <option value="direct" ${settings.tone === "direct" ? "selected" : ""}>Direct</option>
                        </select>
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
                        <div>
                            <label>Response style</label>
                            <select data-field="response_style">
                                <option value="conversational" ${settings.response_style === "conversational" ? "selected" : ""}>Conversational</option>
                                <option value="structured" ${settings.response_style === "structured" ? "selected" : ""}>Structured</option>
                                <option value="sales" ${settings.response_style === "sales" ? "selected" : ""}>Sales-oriented</option>
                                <option value="support" ${settings.response_style === "support" ? "selected" : ""}>Support-oriented</option>
                            </select>
                        </div>
                        <div>
                            <label>Response length</label>
                            <select data-field="response_length">
                                <option value="concise" ${settings.response_length === "concise" ? "selected" : ""}>Concise</option>
                                <option value="balanced" ${settings.response_length === "balanced" ? "selected" : ""}>Balanced</option>
                                <option value="detailed" ${settings.response_length === "detailed" ? "selected" : ""}>Detailed</option>
                            </select>
                        </div>
                    </div>
                    <div>
                        <label>Channel-only instructions</label>
                        <textarea data-field="channel_instructions" rows="5" placeholder="Add behavior specific to this channel only.">${safe(settings.channel_instructions || "")}</textarea>
                        <p class="muted">Business facts and employee knowledge stay shared. This field changes only how the employee behaves on ${safe(xvondCustomerChannelLabel(type))}.</p>
                    </div>
                    <div class="error" data-xvond-generic-settings-error></div>
                    <button type="submit">Save channel settings</button>
                </form>
            </div>
        `;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.querySelector("[data-xvond-generic-settings-close]")?.addEventListener("click", close);
        overlay.addEventListener("click", event => {
            if (event.target === overlay) close();
        });
        const form = overlay.querySelector("[data-xvond-generic-settings-form]");
        form?.addEventListener("submit", async event => {
            event.preventDefault();
            const value = key => form.querySelector(`[data-field="${key}"]`)?.value || "";
            const error = form.querySelector("[data-xvond-generic-settings-error]");
            const submit = form.querySelector('button[type="submit"]');
            if (submit) submit.disabled = true;
            if (error) error.textContent = "";
            try {
                await api(`/customer/agents/${Number(agentId)}/channels/${encodeURIComponent(type)}/settings`, {
                    method: "PUT",
                    body: JSON.stringify({
                        tone: value("tone"),
                        response_style: value("response_style"),
                        response_length: value("response_length"),
                        channel_instructions: value("channel_instructions"),
                    }),
                });
                portalOverview = await api("/customer/overview");
                await renderXvondChannelCenter();
                close();
            } catch (err) {
                if (error) error.textContent = err?.message || "Could not save channel settings.";
            } finally {
                if (submit) submit.disabled = false;
            }
        });
    } catch (error) {
        alert(error.message || "Channel settings are unavailable.");
    }
};
