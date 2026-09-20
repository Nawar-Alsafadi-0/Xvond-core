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
        return `<button type="button" onclick="openCustomerMetaChannelConnect(${Number(agent.id)},'${type}')">${connected ? "Reconnect / Change account" : "Connect with Meta"}</button>`;
    }
    if (type === "website") {
        return `<button type="button" onclick="openPage('agents',[...document.querySelectorAll('#portal-nav .nav-item')].find(x=>x.dataset.page==='agents')||null)">Manage Website Chat</button>`;
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
