let xvondMetaChannelSdkPromise = null;

function xvondLoadMetaChannelSdk(appId, graphVersion) {
    if (typeof xvondCustomerLoadMetaSdk === "function") {
        return xvondCustomerLoadMetaSdk(appId, graphVersion);
    }
    if (window.FB) {
        FB.init({appId, cookie: true, xfbml: false, version: graphVersion || "v26.0", fedCM: false});
        return Promise.resolve();
    }
    if (xvondMetaChannelSdkPromise) return xvondMetaChannelSdkPromise;
    xvondMetaChannelSdkPromise = new Promise((resolve, reject) => {
        window.fbAsyncInit = function () {
            FB.init({appId, cookie: true, xfbml: false, version: graphVersion || "v26.0", fedCM: false});
            resolve();
        };
        const script = document.createElement("script");
        script.id = "facebook-jssdk";
        script.async = true;
        script.defer = true;
        script.crossOrigin = "anonymous";
        script.src = "https://connect.facebook.net/en_US/sdk.js";
        script.onerror = () => reject(new Error("Could not open Meta Connect."));
        document.head.appendChild(script);
    });
    return xvondMetaChannelSdkPromise;
}

function xvondMetaChannelName(type) {
    return type === "instagram" ? "Instagram" : "Facebook Messenger";
}

function xvondChooseMetaAsset(assets, channelType) {
    if (!Array.isArray(assets) || !assets.length) return Promise.resolve(null);
    if (assets.length === 1) return Promise.resolve(assets[0]);

    return new Promise(resolve => {
        const overlay = document.createElement("div");
        overlay.style.cssText = "position:fixed;inset:0;z-index:10000;background:rgba(15,23,42,.65);display:flex;align-items:center;justify-content:center;padding:20px";
        const card = document.createElement("div");
        card.style.cssText = "width:min(520px,100%);background:var(--card,#fff);color:inherit;border-radius:16px;padding:22px;box-shadow:0 24px 70px rgba(0,0,0,.3)";
        card.innerHTML = `
            <h3 style="margin-top:0">Choose ${safe(xvondMetaChannelName(channelType))} account</h3>
            <p class="muted">Choose the account this AI Employee should use.</p>
            <select id="xvond-meta-asset-select" style="width:100%;margin:12px 0">
                ${assets.map((item,index)=>`<option value="${index}">${safe(item.label || item.page_name || item.page_id)}</option>`).join("")}
            </select>
            <div style="display:flex;gap:8px;justify-content:flex-end">
                <button type="button" id="xvond-meta-cancel">Cancel</button>
                <button type="button" id="xvond-meta-confirm">Connect</button>
            </div>
        `;
        overlay.appendChild(card);
        document.body.appendChild(overlay);
        const done = value => { overlay.remove(); resolve(value); };
        card.querySelector("#xvond-meta-cancel").onclick = () => done(null);
        card.querySelector("#xvond-meta-confirm").onclick = () => {
            const index = Number(card.querySelector("#xvond-meta-asset-select").value || 0);
            done(assets[index] || null);
        };
    });
}

async function xvondRefreshCustomerOverview() {
    portalOverview = await api("/customer/overview");
}

window.openCustomerMetaChannelConnect = async function(agentId, channelType) {
    const type = String(channelType || "").toLowerCase();
    try {
        const config = await api(`/customer/meta/channels/connect/config?agent_id=${Number(agentId)}&channel_type=${encodeURIComponent(type)}`);
        if (!config.ready) {
            alert(`${xvondMetaChannelName(type)} connection is not available yet. Contact Xvond support.`);
            return;
        }
        await xvondLoadMetaChannelSdk(config.app_id, config.graph_api_version);
        FB.login(async response => {
            const accessToken = response?.authResponse?.accessToken;
            if (!accessToken) {
                if (response?.status !== "unknown") alert("Meta authorization was not completed.");
                return;
            }
            try {
                const discovered = await api("/customer/meta/channels/connect/discover", {
                    method: "POST",
                    body: JSON.stringify({
                        agent_id: Number(agentId),
                        channel_type: type,
                        user_access_token: String(accessToken),
                    }),
                });
                const asset = await xvondChooseMetaAsset(discovered.assets || [], type);
                if (!asset) {
                    if (!(discovered.assets || []).length) {
                        alert(type === "instagram"
                            ? "No professional Instagram account linked to an available Facebook Page was found."
                            : "No Facebook Page available for Messenger was found.");
                    }
                    return;
                }
                const result = await api("/customer/meta/channels/connect/complete", {
                    method: "POST",
                    body: JSON.stringify({
                        agent_id: Number(agentId),
                        channel_type: type,
                        user_access_token: String(accessToken),
                        page_id: String(asset.page_id),
                    }),
                });
                await xvondRefreshCustomerOverview();
                await loadAgents();
                alert(result.ready_for_launch
                    ? `${xvondMetaChannelName(type)} connected successfully. Xvond can now complete launch verification.`
                    : `${xvondMetaChannelName(type)} connected. Xvond will complete the remaining launch checks.`);
            } catch (error) {
                alert(error.message || "Meta connection failed.");
            }
        }, {
            scope: (config.scopes || []).join(","),
            return_scopes: true,
            auth_type: "rerequest",
        });
    } catch (error) {
        alert(error.message || "Could not start Meta Connect.");
    }
};

function xvondDecorateAgentsWithMetaChannels() {
    const cards = Array.from(document.querySelectorAll("#agents-list .agent"));
    (agents || []).forEach((agent, index) => {
        const card = cards[index];
        if (!card) return;
        const channels = (portalOverview?.channels || []).filter(item =>
            Number(item.agent_id) === Number(agent.id) &&
            ["instagram", "messenger"].includes(String(item.type || "").toLowerCase())
        );
        for (const channel of channels) {
            const type = String(channel.type || "").toLowerCase();
            if (card.querySelector(`[data-xvond-meta-connect="${type}"]`)) continue;
            const connected = String(channel.provisioning_state || "").toLowerCase() === "connected";
            const box = document.createElement("div");
            box.dataset.xvondMetaConnect = type;
            box.style.cssText = "margin-top:10px;padding:12px;border:1px solid rgba(148,163,184,.25);border-radius:10px";
            box.innerHTML = `
                <strong>${safe(xvondMetaChannelName(type))}</strong>
                <p class="muted" style="margin:6px 0 10px">${connected ? "Connected to Meta through Xvond." : "Connect the business account securely with Meta. No access token needs to be copied manually."}</p>
                <button type="button" onclick="openCustomerMetaChannelConnect(${Number(agent.id)},'${type}')">
                    ${connected ? "Reconnect / Change Account" : "Connect with Meta"}
                </button>
            `;
            card.appendChild(box);
        }
    });
}

if (typeof loadAgents === "function") {
    const xvondMetaChannelsOriginalLoadAgents = loadAgents;
    loadAgents = async function (...args) {
        const result = await xvondMetaChannelsOriginalLoadAgents(...args);
        xvondDecorateAgentsWithMetaChannels();
        return result;
    };
}
