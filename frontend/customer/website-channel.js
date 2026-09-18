function xvondCustomerWebsiteStatus(config) {
    if (config?.enabled) {
        return {
            title: "Website Chat Live",
            detail: "The website widget is active and uses this AI employee.",
            action: null,
        };
    }
    if (config?.prepared) {
        return {
            title: "Website Chat prepared",
            detail: "The website is configured. It will become live together with the employee when you launch.",
            action: "Edit Website setup",
        };
    }
    return {
        title: "Set up Website Chat",
        detail: "Add the website domain that will host the Xvond chat widget.",
        action: "Set up Website Chat",
    };
}

function xvondCustomerWebsiteForm(box, agentId, config) {
    const form = box.querySelector(".xvond-website-form");
    if (!form) return;
    const values = config?.config || {};
    const domain = form.querySelector('[data-field="allowed_domain"]');
    const name = form.querySelector('[data-field="widget_name"]');
    const welcome = form.querySelector('[data-field="welcome_message"]');
    const welcomeEn = form.querySelector('[data-field="welcome_message_en"]');
    if (domain) domain.value = values.allowed_domain || "";
    if (name) name.value = values.widget_name || "";
    if (welcome) welcome.value = values.welcome_message || "";
    if (welcomeEn) welcomeEn.value = values.welcome_message_en || "";

    form.addEventListener("submit", async event => {
        event.preventDefault();
        const submit = form.querySelector('button[type="submit"]');
        const error = form.querySelector(".xvond-website-error");
        if (submit) submit.disabled = true;
        if (error) error.textContent = "";
        try {
            await api(`/customer/website-channel/agents/${Number(agentId)}`, {
                method: "PUT",
                body: JSON.stringify({
                    allowed_domain: domain?.value?.trim() || "",
                    widget_name: name?.value?.trim() || null,
                    welcome_message: welcome?.value?.trim() || null,
                    welcome_message_en: welcomeEn?.value?.trim() || null,
                }),
            });
            await loadAgents();
        } catch (err) {
            if (error) error.textContent = err?.message || "Could not save Website Chat setup.";
        } finally {
            if (submit) submit.disabled = false;
        }
    });
}

async function xvondDecorateCustomerAgentsWithWebsite() {
    if (portalOverview?.company?.onboarding_source !== "self_service") return;
    if (!currentUser || !["owner", "admin", "manager"].includes(currentUser.role)) return;

    const cards = Array.from(document.querySelectorAll("#agents-list .agent"));
    await Promise.all((agents || []).map(async (agent, index) => {
        const card = cards[index];
        if (!card || card.querySelector(".xvond-website-connect")) return;

        const box = document.createElement("div");
        box.className = "xvond-website-connect";
        box.style.marginTop = "14px";
        box.style.paddingTop = "12px";
        box.style.borderTop = "1px solid rgba(148,163,184,.25)";
        box.innerHTML = '<p class="muted" style="margin:0">Checking Website Chat setup...</p>';
        card.appendChild(box);

        try {
            const config = await api(`/customer/website-channel/agents/${Number(agent.id)}`);
            const status = xvondCustomerWebsiteStatus(config);
            box.innerHTML = `
                <p style="margin:0 0 6px"><strong>${safe(status.title)}</strong></p>
                <p class="muted" style="margin:0 0 8px">${safe(status.detail)}</p>
                ${config?.configured ? `
                    <p class="muted" style="margin:0 0 8px">Domain: <strong>${safe(config.config?.allowed_domain || "")}</strong></p>
                ` : ""}
                ${config?.embed_code ? `
                    <div style="margin-top:10px">
                        <label><strong>Embed code</strong></label>
                        <textarea class="xvond-website-embed" readonly rows="2"></textarea>
                        <button type="button" class="xvond-website-copy" style="margin-top:6px">Copy embed code</button>
                    </div>
                ` : ""}
                ${status.action && config?.can_edit ? `
                    <button type="button" class="xvond-website-toggle" style="margin-top:10px">${safe(status.action)}</button>
                    <form class="xvond-website-form hidden" style="margin-top:12px">
                        <label>Website domain</label>
                        <input data-field="allowed_domain" placeholder="example.com" required>
                        <label style="display:block;margin-top:8px">Widget name <span class="muted">(optional)</span></label>
                        <input data-field="widget_name" placeholder="Customer Assistant">
                        <label style="display:block;margin-top:8px">Welcome message <span class="muted">(Arabic)</span></label>
                        <input data-field="welcome_message">
                        <label style="display:block;margin-top:8px">Welcome message <span class="muted">(English)</span></label>
                        <input data-field="welcome_message_en">
                        <button type="submit" style="margin-top:10px">Save Website setup</button>
                        <div class="error xvond-website-error"></div>
                    </form>
                ` : ""}
                ${!config?.can_edit && !config?.enabled ? '<p class="muted" style="margin-top:8px">Deactivate the employee before changing Website Chat setup.</p>' : ""}
            `;

            const embed = box.querySelector(".xvond-website-embed");
            if (embed) embed.value = config.embed_code || "";
            const copy = box.querySelector(".xvond-website-copy");
            if (copy && embed) {
                copy.addEventListener("click", async () => {
                    try {
                        await navigator.clipboard.writeText(embed.value);
                        copy.textContent = "Copied";
                    } catch (_) {
                        embed.select();
                    }
                });
            }

            const toggle = box.querySelector(".xvond-website-toggle");
            const form = box.querySelector(".xvond-website-form");
            if (toggle && form) {
                toggle.addEventListener("click", () => form.classList.toggle("hidden"));
            }
            xvondCustomerWebsiteForm(box, agent.id, config);
        } catch (error) {
            box.innerHTML = '<p class="muted" style="margin:0">Website Chat setup is unavailable right now.</p>';
        }
    }));
}

if (typeof loadAgents === "function") {
    const xvondWebsiteOriginalLoadAgents = loadAgents;
    loadAgents = async function (...args) {
        const result = await xvondWebsiteOriginalLoadAgents(...args);
        await xvondDecorateCustomerAgentsWithWebsite();
        return result;
    };
}
