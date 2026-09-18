(() => {
  const sourceLabel = value => value === "self_service" ? "Self-service" : "Managed by Xvond";
  const sourceClass = value => value === "self_service" ? "status-active" : "status-pending";
  const state = {
    source: "managed",
    lifecycle: "",
    search: "",
    offset: 0,
    limit: 50,
    total: 0,
  };
  let searchTimer = null;

  function directoryCopy(source) {
    if (source === "self_service") {
      return {
        title: "Self-Service",
        subtitle: "Workspaces created by customers through the AI Employee Builder. Xvond monitors platform health without turning them into managed-delivery projects.",
      };
    }
    if (source === "all") {
      return {
        title: "All Companies",
        subtitle: "Unified operational index across both delivery models.",
      };
    }
    return {
      title: "Managed Delivery",
      subtitle: "Enterprise customers whose AI employees are designed, configured and delivered by Xvond.",
    };
  }

  function setActiveSourceNav(source) {
    document.querySelectorAll(".nav-item").forEach(item => item.classList.remove("active"));
    const item = document.querySelector(`.nav-item[data-company-source="${source}"]`);
    if (item) item.classList.add("active");
  }

  function syncDirectoryChrome() {
    const copy = directoryCopy(state.source);
    const title = document.getElementById("company-directory-title");
    const subtitle = document.getElementById("company-directory-subtitle");
    const source = document.getElementById("company-source-filter");
    const lifecycle = document.getElementById("company-lifecycle-filter");
    const search = document.getElementById("company-search");
    const create = document.getElementById("new-managed-company");
    if (title) title.textContent = copy.title;
    if (subtitle) subtitle.textContent = copy.subtitle;
    if (source) source.value = state.source;
    if (lifecycle) lifecycle.value = state.lifecycle;
    if (search && search.value !== state.search) search.value = state.search;
    if (create) {
      create.classList.toggle("hidden", state.source !== "managed" || xvondSupportMode());
    }
    document.getElementById("page-title").textContent = copy.title;
    if (state.source === "managed" || state.source === "self_service") setActiveSourceNav(state.source);
  }

  function queryString() {
    const params = new URLSearchParams();
    if (state.source !== "all") params.set("source", state.source);
    if (state.lifecycle) params.set("lifecycle", state.lifecycle);
    if (state.search) params.set("search", state.search);
    params.set("limit", String(state.limit));
    params.set("offset", String(state.offset));
    return params.toString();
  }

  function renderCompanies() {
    const body = document.getElementById("companies-table");
    if (!body) return;
    if (!companiesCache.length) {
      body.innerHTML = '<tr><td colspan="7"><div class="workspace-empty"><strong>No companies found</strong><p>Try another search or filter.</p></div></td></tr>';
      return;
    }
    body.innerHTML = companiesCache.map(company => `
      <tr>
        <td>${Number(company.id)}</td>
        <td><strong>${escapeAdmin(company.name)}</strong></td>
        <td><span class="status ${sourceClass(company.onboarding_source)}">${escapeAdmin(sourceLabel(company.onboarding_source))}</span></td>
        <td><span class="status ${adminLifecycleClass(company.lifecycle_status)}">${escapeAdmin(adminLifecycleLabel(company.lifecycle_status))}</span></td>
        <td><span class="status ${company.active ? "status-active" : "status-inactive"}">${company.active ? "Running" : "Stopped"}</span></td>
        <td>${company.created_at ? new Date(company.created_at).toLocaleDateString() : ""}</td>
        <td><button class="table-button" onclick="${xvondSupportMode() ? `openSupportCompany(${Number(company.id)})` : `openCompany(${Number(company.id)})`}">${xvondSupportMode() ? "Inspect" : "Open"}</button></td>
      </tr>`).join("");
  }

  function renderPagination() {
    const meta = document.getElementById("company-directory-meta");
    const pager = document.getElementById("company-directory-pagination");
    const start = state.total ? state.offset + 1 : 0;
    const end = Math.min(state.offset + state.limit, state.total);
    if (meta) meta.textContent = `${state.total.toLocaleString()} companies · showing ${start.toLocaleString()}–${end.toLocaleString()}`;
    if (!pager) return;
    const previous = state.offset > 0;
    const next = state.offset + state.limit < state.total;
    pager.innerHTML = `
      <button class="table-button" ${previous ? "" : "disabled"} onclick="changeCompanyDirectoryPage(-1)">Previous</button>
      <span class="meta">Page ${Math.floor(state.offset / state.limit) + 1} of ${Math.max(1, Math.ceil(state.total / state.limit))}</span>
      <button class="table-button" ${next ? "" : "disabled"} onclick="changeCompanyDirectoryPage(1)">Next</button>`;
  }

  async function loadDirectory() {
    syncDirectoryChrome();
    const body = document.getElementById("companies-table");
    if (body) body.innerHTML = '<tr><td colspan="7"><div class="workspace-empty"><strong>Loading companies…</strong></div></td></tr>';
    try {
      const data = await api(`/admin/companies?${queryString()}`);
      companiesCache = data.companies || [];
      state.total = Number(data.total ?? companiesCache.length);
      renderCompanies();
      renderPagination();
    } catch (error) {
      console.error(error);
      companiesCache = [];
      state.total = 0;
      if (body) body.innerHTML = `<tr><td colspan="7"><div class="error">${escapeAdmin(error.message)}</div></td></tr>`;
      renderPagination();
    }
  }

  window.loadCompanies = loadDirectory;

  window.showCompanyDirectory = async function showCompanyDirectory(source = "managed", button = null) {
    state.source = ["managed", "self_service", "all"].includes(source) ? source : "managed";
    state.offset = 0;
    const selectedButton = button || document.querySelector(`.nav-item[data-company-source="${state.source}"]`);
    await showPage("companies", selectedButton);
  };

  window.returnToCompanyDirectory = function returnToCompanyDirectory() {
    showCompanyDirectory(state.source);
  };

  window.changeCompanyDirectoryPage = function changeCompanyDirectoryPage(direction) {
    const next = Math.max(0, state.offset + Number(direction || 0) * state.limit);
    if (next >= state.total && direction > 0) return;
    state.offset = next;
    loadDirectory();
  };

  window.addEventListener("DOMContentLoaded", () => {
    document.getElementById("company-source-filter")?.addEventListener("change", event => {
      state.source = event.target.value || "managed";
      state.offset = 0;
      loadDirectory();
    });
    document.getElementById("company-lifecycle-filter")?.addEventListener("change", event => {
      state.lifecycle = event.target.value || "";
      state.offset = 0;
      loadDirectory();
    });
    document.getElementById("company-search")?.addEventListener("input", event => {
      state.search = event.target.value.trim();
      state.offset = 0;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(loadDirectory, 250);
    });
  });
})();
