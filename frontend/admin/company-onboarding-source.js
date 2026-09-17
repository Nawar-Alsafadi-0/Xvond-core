(() => {
  const sourceLabel = value => value === "self_service" ? "Self-service" : "Managed by Xvond";
  const sourceClass = value => value === "self_service" ? "status-active" : "status-pending";
  let sourceCompanies = [];

  function filterValue() {
    return document.getElementById("company-source-filter")?.value || "all";
  }

  function renderCompanies() {
    const body = document.getElementById("companies-table");
    if (!body) return;
    const selected = filterValue();
    const rows = sourceCompanies.filter(company => selected === "all" || (company.onboarding_source || "managed") === selected);
    body.innerHTML = rows.map(company => `
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

  async function loadSourcesAndRender() {
    try {
      const data = await api("/admin/companies");
      sourceCompanies = data.companies || [];
      renderCompanies();
    } catch (error) {
      console.error(error);
    }
  }

  const originalLoadCompanies = window.loadCompanies;
  if (typeof originalLoadCompanies === "function") {
    window.loadCompanies = async function loadCompaniesWithSource() {
      await originalLoadCompanies();
      await loadSourcesAndRender();
    };
  }

  document.getElementById("company-source-filter")?.addEventListener("change", renderCompanies);
})();
