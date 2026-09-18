let xvondCustomerMetaSignupState = null;
let xvondCustomerMetaSignupMessage = null;
let xvondCustomerMetaSdkPromise = null;

function xvondCustomerTrustedMetaOrigin(origin) {
    try {
        const url = new URL(origin);
        const host = (url.hostname || "").toLowerCase();
        return url.protocol === "https:" && (host === "facebook.com" || host.endsWith(".facebook.com"));
    } catch (_) {
        return false;
    }
}

function xvondCustomerLoadMetaSdk(appId, graphVersion) {
    // WhatsApp Embedded Signup needs provider-side identifiers and a code
    // response, but those implementation details stay hidden from customers.
    if (window.FB) {
        FB.init({appId, cookie: true, xfbml: false, version: graphVersion || "v26.0", fedCM: false});
        return Promise.resolve();
    }
    if (xvondCustomerMetaSdkPromise) return xvondCustomerMetaSdkPromise;
    xvondCustomerMetaSdkPromise = new Promise((resolve, reject) => {
        window.fbAsyncInit = function () {
            FB.init({appId, cookie: true, xfbml: false, version: graphVersion || "v26.0", fedCM: false});
            resolve();
        };
        const existing = document.getElementById("facebook-jssdk");
        if (existing) {
            existing.addEventListener("load", () => resolve(), {once: true});
            return;
        }
        const script = document.createElement("script");
        script.id = "facebook-jssdk";
        script.async = true;
        script.defer = true;
        script.crossOrigin = "anonymous";
        script.src = "https://connect.facebook.net/en_US/sdk.js";
        script.onerror = () => reject(new Error("تعذر فتح نافذة ربط واتساب. حاول مرة أخرى."));
        document.head.appendChild(script);
    });
    return xvondCustomerMetaSdkPromise;
}

function xvondCustomerMetaLoginOptions(config) {
    const extras = {setup: {}};
    if (config.feature_type) extras.featureType = config.feature_type;
    if (config.session_info_version) extras.sessionInfoVersion = String(config.session_info_version);
    return {
        config_id: config.config_id,
        response_type: "code",
        override_default_response_type: true,
        extras,
    };
}

function xvondCustomerWhatsAppStatus(config) {
    if (!config.connected) {
        if (config.coexistence && config.connection_status === "coexistence_setup_required") {
            return {
                title: "ربط واتساب موجود · إعداد التعايش غير مكتمل",
                detail: "Xvond لم يتحقق بعد من اشتراكات Meta المطلوبة لرسائل العملاء وردود تطبيق WhatsApp Business. لن يبدأ الموظف حتى تكتمل اشتراكات الرسائل والتحكم البشري.",
                label: "إعادة الربط لإكمال الإعداد",
            };
        }
        const needsReconnect = config.configured || config.connection_status === "invalid_token";
        return {
            title: needsReconnect ? "اتصال واتساب يحتاج إعادة ربط" : "اربط رقم واتساب",
            detail: needsReconnect
                ? "أعد تأكيد الحساب والرقم حتى يعود الموظف للعمل على واتساب."
                : "اربط رقم WhatsApp Business الخاص بشركتك بالموظف. ستُفتح نافذة آمنة لتأكيد ملكية الحساب والرقم.",
            label: needsReconnect ? "إعادة ربط الرقم" : "ربط رقم واتساب",
        };
    }

    const phone = safe(config.display_phone_number || "الرقم متصل");
    const name = config.verified_name ? ` · ${safe(config.verified_name)}` : "";
    if (config.coexistence && config.coexistence_ready === false) {
        return {
            title: `واتساب متصل · ${phone}${name}`,
            detail: config.runtime_ready
                ? "الموظف AI يستطيع الرد الآن. بقي اختبار واحد للتحكم البشري: أرسل ردًا يدويًا من تطبيق WhatsApp Business على محادثة عميل، وسيتوقف الـAI تلقائيًا عن الرد على تلك المحادثة عند وصول الحدث."
                : "رقم واتساب متصل، لكن الموظف يحتاج أيضًا إلى إكمال إعداد الخدمة. بعد ذلك اختبر ردًا يدويًا واحدًا من تطبيق WhatsApp Business للتحقق من التحويل التلقائي للبشر.",
            label: "تغيير الرقم أو إعادة الربط",
        };
    }

    if (config.runtime_ready) {
        return {
            title: `واتساب متصل · ${phone}${name}`,
            detail: config.coexistence
                ? "الموظف AI جاهز على نفس رقم WhatsApp Business، والتحويل التلقائي للبشر تم التحقق منه."
                : "الموظف AI جاهز لاستقبال رسائل العملاء على هذا الرقم.",
            label: "تغيير الرقم أو إعادة الربط",
        };
    }

    return {
        title: `تم ربط الرقم · ${phone}${name}`,
        detail: "الرقم مربوط بنجاح. يحتاج الموظف إلى إكمال بعض إعدادات الخدمة من Xvond قبل بدء الرد على العملاء.",
        label: "إعادة ربط الرقم",
    };
}

function xvondCustomerWhatsAppBlockers(config) {
    const blockers = Array.isArray(config.blockers) ? config.blockers.filter(Boolean) : [];
    if (config.connected && config.coexistence && config.coexistence_ready === false) {
        return `
            <div class="muted" style="margin-top:8px">
                <strong>التحكم البشري:</strong> الاتصال يعمل، لكن لم يصل بعد رد يدوي من تطبيق WhatsApp Business. أول رد بشري حقيقي سيؤكد أن Xvond يستقبل أحداث التعايش ويوقف الـAI على المحادثة نفسها.
            </div>
        `;
    }
    if (!config.connected && config.connection_status === "coexistence_setup_required") {
        return `
            <div class="muted" style="margin-top:8px">
                <strong>حالة Meta:</strong> اشتراكات Coexistence المطلوبة غير مكتملة أو لم يتم التحقق منها بعد.
            </div>
        `;
    }
    if (!config.connected || blockers.length === 0) return "";
    return `
        <div class="muted" style="margin-top:8px">
            <strong>حالة التشغيل:</strong> يحتاج الموظف إلى إكمال الإعداد من Xvond قبل بدء الرد.
        </div>
    `;
}

function xvondCustomerWhatsAppIntro() {
    return `
        <div class="xvond-managed-whatsapp-note" style="margin:0 0 12px;padding:12px;border:1px solid rgba(148,163,184,.25);border-radius:10px">
            <strong>خدمة واتساب مُدارة من Xvond</strong>
            <p class="muted" style="margin:6px 0 0">
                اشتراك الموظف وإدارته يتمان عبر Xvond. نافذة الربط مخصصة لتأكيد ملكية حساب ورقم واتساب وربطه بالموظف.
            </p>
        </div>
    `;
}

window.addEventListener("message", event => {
    if (!xvondCustomerTrustedMetaOrigin(event.origin)) return;
    let payload = event.data;
    if (typeof payload === "string") {
        try { payload = JSON.parse(payload); } catch (_) { return; }
    }
    if (!payload || payload.type !== "WA_EMBEDDED_SIGNUP") return;
    const completedEvents = new Set([
        "FINISH",
        "FINISH_ONLY_WABA",
        "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"
    ]);
    if (completedEvents.has(payload.event)) {
        xvondCustomerMetaSignupMessage = {...(payload.data || {}), event: payload.event};
    }
});

window.openCustomerMetaWhatsAppConnect = async function (agentId) {
    try {
        const config = await api(`/customer/meta/whatsapp/embedded-signup/config?agent_id=${Number(agentId)}`);
        if (!config.ready) {
            alert("ربط واتساب غير متاح حاليًا. تواصل مع Xvond لإكمال إعداد الخدمة.");
            return;
        }
        if (config.can_edit === false) {
            alert("أوقف الموظف أولًا قبل تغيير اتصال واتساب.");
            return;
        }

        xvondCustomerMetaSignupState = {agentId: Number(agentId)};
        xvondCustomerMetaSignupMessage = null;
        await xvondCustomerLoadMetaSdk(config.app_id, config.graph_api_version);
        FB.login(response => {
            const code = response?.authResponse?.code;
            if (!code) {
                if (response?.status !== "unknown") alert("لم يكتمل تأكيد حساب واتساب. حاول مرة أخرى.");
                return;
            }
            xvondCustomerFinishMetaWhatsAppSignup(code);
        }, xvondCustomerMetaLoginOptions(config));
    } catch (error) {
        alert(error.message || "تعذر بدء ربط واتساب. حاول مرة أخرى.");
    }
};

async function xvondCustomerFinishMetaWhatsAppSignup(code) {
    try {
        for (let attempt = 0; attempt < 40 && !xvondCustomerMetaSignupMessage; attempt += 1) {
            await new Promise(resolve => setTimeout(resolve, 250));
        }

        const data = xvondCustomerMetaSignupMessage || {};
        const wabaId = data.waba_id || data.wabaId;
        const phoneNumberId = data.phone_number_id || data.phoneNumberId || null;
        const businessId = data.business_id || data.businessId || null;
        if (!wabaId) {
            alert("لم يكتمل ربط حساب واتساب. أكمل نافذة التحقق حتى النهاية ثم حاول مجددًا.");
            return;
        }

        const result = await api("/customer/meta/whatsapp/embedded-signup/complete", {
            method: "POST",
            body: JSON.stringify({
                agent_id: xvondCustomerMetaSignupState.agentId,
                code: String(code),
                waba_id: String(wabaId),
                phone_number_id: phoneNumberId ? String(phoneNumberId) : null,
                business_id: businessId ? String(businessId) : null,
                connection_mode: data.event === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"
                    ? "coexistence"
                    : "embedded_signup"
            })
        });

        if (result.runtime_ready) {
            if (result.coexistence && result.coexistence_ready === false) {
                alert(`تم ربط رقم واتساب بنجاح.\n${result.display_phone_number || ""}\nالموظف AI يستطيع الرد الآن. لاختبار التحويل للبشر، أرسل ردًا يدويًا واحدًا من تطبيق WhatsApp Business على محادثة عميل.`);
            } else {
                const mode = result.coexistence
                    ? "الموظف AI جاهز، والتحويل التلقائي للبشر تم التحقق منه."
                    : "الموظف AI جاهز على واتساب.";
                alert(`تم ربط رقم واتساب بنجاح.\n${result.display_phone_number || ""}\n${mode}`);
            }
        } else if (result.coexistence) {
            alert("تم ربط رقم واتساب. أكمل إعداد الموظف من Xvond، وبعدها أرسل ردًا يدويًا من تطبيق WhatsApp Business على محادثة عميل لاختبار التحويل التلقائي للبشر.");
        } else {
            alert("تم ربط رقم واتساب بنجاح. سيبدأ الموظف بالعمل بعد إكمال إعداد الخدمة من Xvond.");
        }
        await loadAgents();
    } catch (error) {
        alert(error.message || "تعذر إكمال ربط واتساب. حاول مرة أخرى أو تواصل مع Xvond.");
    } finally {
        xvondCustomerMetaSignupState = null;
        xvondCustomerMetaSignupMessage = null;
    }
}

async function xvondDecorateCustomerAgentsWithWhatsApp() {
    if (!currentUser || !["owner", "admin", "manager"].includes(currentUser.role)) return;
    const cards = Array.from(document.querySelectorAll("#agents-list .agent"));
    await Promise.all((agents || []).map(async (agent, index) => {
        const card = cards[index];
        if (!card || card.querySelector(".xvond-whatsapp-connect")) return;

        const box = document.createElement("div");
        box.className = "xvond-whatsapp-connect";
        box.style.marginTop = "14px";
        box.style.paddingTop = "12px";
        box.style.borderTop = "1px solid rgba(148,163,184,.25)";
        box.innerHTML = `<p class="muted" style="margin:0">جاري فحص اتصال واتساب...</p>`;
        card.appendChild(box);

        try {
            const config = await api(`/customer/meta/whatsapp/embedded-signup/config?agent_id=${Number(agent.id)}`);
            const status = xvondCustomerWhatsAppStatus(config);
            box.innerHTML = `
                ${xvondCustomerWhatsAppIntro()}
                <p style="margin:0 0 6px"><strong>${status.title}</strong></p>
                <p class="muted" style="margin:0 0 8px">${status.detail}</p>
                ${xvondCustomerWhatsAppBlockers(config)}
                <button type="button" style="margin-top:10px" onclick="openCustomerMetaWhatsAppConnect(${Number(agent.id)})" ${(config.ready && config.can_edit !== false) ? "" : "disabled"}>${status.label}</button>
                ${config.ready ? "" : `<p class="muted" style="margin:8px 0 0">ربط واتساب يحتاج تفعيلًا من فريق Xvond.</p>`}
                ${config.can_edit === false ? `<p class="muted" style="margin:8px 0 0">أوقف الموظف أولًا قبل تغيير الرقم أو إعادة ربط واتساب.</p>` : ""}
            `;
        } catch (error) {
            box.innerHTML = `<p class="muted" style="margin:0">تعذر فحص اتصال واتساب. تواصل مع Xvond إذا استمرت المشكلة.</p>`;
        }
    }));
}

if (typeof loadAgents === "function") {
    const xvondOriginalCustomerLoadAgents = loadAgents;
    loadAgents = async function (...args) {
        const result = await xvondOriginalCustomerLoadAgents(...args);
        await xvondDecorateCustomerAgentsWithWhatsApp();
        return result;
    };
}
