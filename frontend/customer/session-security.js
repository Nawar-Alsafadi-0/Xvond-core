// Browser authentication uses the server-issued HttpOnly xvond_session cookie.
// Keep bearer-token support on the API for non-browser clients, but never persist
// a bearer token in JavaScript-accessible storage in the bundled portal.
localStorage.removeItem("xvond_customer_token");
token = null;

api = async function(path, options = {}) {
    const headers = {
        "Content-Type": "application/json",
        ...(options.headers || {})
    };
    const response = await fetch(path, {
        ...options,
        credentials: "same-origin",
        headers
    });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (response.status === 401) {
        // Login failures are credential errors, not expired-session errors.
        if (path === "/auth/login") {
            throw new Error(data.detail || "Invalid email or password");
        }
        clearSession();
        throw new Error("Session expired. Please sign in again.");
    }
    if (!response.ok) {
        const detail = typeof data.detail === "string"
            ? data.detail
            : (data.detail?.message || "Request failed");
        throw new Error(detail);
    }
    return data;
};

// Customer eligibility and company attachment are enforced server-side by
// /customer/overview. Resolve authentication first so an expired session does
// not fire multiple protected requests and produce duplicate 401 errors.
startPortal = async function(options = {}) {
    try {
        currentUser = await api("/users/me");
        portalOverview = await api("/customer/overview");
        portalNavigation = portalOverview?.portal?.navigation || fallbackPortalNavigation();
        document.getElementById("login-screen").classList.add("hidden");
        document.getElementById("portal").classList.remove("hidden");
        document.getElementById("user-email").textContent = currentUser.email;
        renderPortalNavigation();
        renderAccountInfo();
        renderDashboard();
        await openInitialPortalPage();
    } catch (err) {
        clearSession();
        const error = document.getElementById("login-error");
        if (error) {
            error.textContent = options.silentAuthFailure && String(err.message).startsWith("Session expired")
                ? ""
                : err.message;
        }
        throw err;
    }
};

login = async function() {
    const email = document.getElementById("login-email").value.trim();
    const password = document.getElementById("login-password").value;
    const error = document.getElementById("login-error");
    error.textContent = "";
    try {
        const data = await api("/auth/login", {
            method: "POST",
            body: JSON.stringify({email, password})
        });
        if (!["owner", "admin", "manager", "employee"].includes(data.user?.role)) {
            try { await api("/auth/logout", {method: "POST", body: "{}"}); } catch (_) {}
            throw new Error("Customer account required");
        }
        token = null;
        await startPortal();
    } catch (err) {
        error.textContent = err.message;
    }
};

logout = async function() {
    try {
        await api("/auth/logout", {method: "POST", body: "{}"});
    } catch (_) {
        // Clear the browser state even when the server session has expired.
    } finally {
        clearSession();
    }
};

// app.js no longer resumes from localStorage because the legacy token is
// removed before it loads. Resume from the HttpOnly cookie instead. A missing
// cookie on the initial public login screen is expected and should stay silent.
startPortal({silentAuthFailure: true}).catch(() => {});
