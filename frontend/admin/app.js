const API="";
let token=null;
let companiesCache=[];
let agentTestConversationId=null;
let currentAdminUser=null;
const XVOND_ADMIN_UI_ROLES=new Set(["super_admin","xvond_admin","support"]);
const XVOND_ADMIN_WORKSPACE_TABS=new Set([
  "overview","company","capabilities","agents","knowledge","channels",
  "operations","integrations","conversations","usage","users","billing","logs"
]);

// Remove browser-readable bearer tokens left by older Xvond builds.
localStorage.removeItem("xvond_admin_token");
localStorage.removeItem("xvond_admin_user");

function escapeAdmin(value){return String(value??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#039;")}
function escapeProduction(value){return escapeAdmin(value)}
function authHeaders(){return {"Content-Type":"application/json"}}
function clearAdminSession(){token=null;currentAdminUser=null;document.body.dataset.xvondRole="";document.getElementById("app")?.classList.add("hidden");document.getElementById("login-screen")?.classList.remove("hidden")}
function adminNumber(value){const n=Number(value||0);return Number.isFinite(n)?n.toLocaleString():String(value??0)}
function adminMoney(value){const n=Number(value||0);return Number.isFinite(n)?n.toFixed(3):"0.000"}
function adminLifecycleLabel(value){const status=String(value||"onboarding").toLowerCase();const labels={onboarding:"Onboarding",testing:"Testing",live:"Live",paused:"Paused",suspended:"Suspended",cancelled:"Cancelled",archived:"Archived"};return labels[status]||status}
function adminLifecycleClass(value){const status=String(value||"onboarding").toLowerCase();return status==="live"?"status-active":["suspended","cancelled","archived"].includes(status)?"status-inactive":"status-pending"}
function xvondSupportMode(){return currentAdminUser?.role==="support"}
function adminWorkspaceTab(value){const tab=String(value||"overview").trim().toLowerCase();return XVOND_ADMIN_WORKSPACE_TABS.has(tab)?tab:"overview"}

async function api(url,options={}){
  const response=await fetch(API+url,{...options,credentials:"same-origin",headers:{...(options.headers||{}),"Content-Type":"application/json"}});
  let data={};try{data=await response.json()}catch(_e){}
  if(response.status===401){clearAdminSession();throw new Error("Authentication expired")}
  if(!response.ok){const detail=typeof data.detail==='string'?data.detail:(data.detail?.message||"Request failed");throw new Error(detail)}
  return data;
}

async function login(){
  const email=document.getElementById("login-email").value.trim(),password=document.getElementById("login-password").value,error=document.getElementById("login-error");error.textContent="";
  try{
    const data=await api("/auth/login",{method:"POST",body:JSON.stringify({email,password})});
    if(!XVOND_ADMIN_UI_ROLES.has(data.user?.role)){
      try{await api("/auth/logout",{method:"POST",body:"{}"})}catch(_e){}
      throw new Error("Xvond internal account required");
    }
    startApp(data.user);
  }catch(e){error.textContent=e.message}
}

async function logout(){try{await api("/auth/logout",{method:"POST",body:"{}"})}catch(_e){}finally{clearAdminSession()}}

function startApp(user={}){
  currentAdminUser=user;document.body.dataset.xvondRole=user.role||"";
  document.getElementById("login-screen").classList.add("hidden");document.getElementById("app").classList.remove("hidden");
  document.getElementById("current-user").textContent=user.role==="support"?`${user.email||"Xvond Support"} · Read only`:(user.email||"Xvond Admin");
  const createButton=document.querySelector('#page-companies .section-header > button');if(createButton)createButton.classList.toggle('hidden',xvondSupportMode());
  showPage("dashboard",document.querySelector('.nav-item'));
}

async function resumeAdminSession(){
  try{
    const user=await api("/users/me");
    if(!XVOND_ADMIN_UI_ROLES.has(user?.role)){clearAdminSession();return}
    startApp(user);
  }catch(_e){clearAdminSession()}
}

async function showPage(name,button=null){
  document.querySelectorAll(".page").forEach(item=>item.classList.add("hidden"));const page=document.getElementById(`page-${name}`);if(page)page.classList.remove("hidden");
  document.querySelectorAll(".nav-item").forEach(item=>item.classList.remove("active"));if(button)button.classList.add("active");
  document.getElementById("page-title").textContent=name==="companies"?"Companies":name==="company-detail"?"Company":"Dashboard";
  if(name==="dashboard")await loadDashboard();if(name==="companies")await loadCompanies();
}

function renderAdminAttention(data){
  const target=document.getElementById("dashboard-attention");if(!target)return;
  const items=data.attention_items||[];
  if(!items.length){target.innerHTML='<div class="status status-active" style="display:inline-block">No operational incidents require review</div>';return}
  target.innerHTML=`<div class="operation-list">${items.map(item=>{
    const companyId=Number(item.company_id);
    const safeCompanyId=Number.isInteger(companyId)&&companyId>0?companyId:0;
    const tab=adminWorkspaceTab(item.tab);
    return `<div class="request-card"><div class="request-card-head"><div><strong>${escapeAdmin(item.company_name||`Company #${item.company_id}`)}</strong><div class="meta">${escapeAdmin(item.title||item.type)} · ${adminNumber(item.count)} event${Number(item.count)===1?'':'s'}</div></div><span class="status ${item.severity==='critical'?'status-inactive':'status-active'}">${escapeAdmin(item.severity||'review')}</span></div><button class="table-button admin-attention-open" data-company-id="${safeCompanyId}" data-workspace-tab="${escapeAdmin(tab)}">${xvondSupportMode()?'Inspect':'Open Workspace'}</button></div>`;
  }).join('')}</div>`;
  target.querySelectorAll(".admin-attention-open").forEach(button=>{
    button.addEventListener("click",()=>{
      const companyId=Number(button.dataset.companyId);
      if(!Number.isInteger(companyId)||companyId<=0)return;
      if(xvondSupportMode())openSupportCompany(companyId);
      else loadCompanyControlCenter(companyId,adminWorkspaceTab(button.dataset.workspaceTab));
    });
  });
}

async function loadDashboard(){
  try{
    const [data,worker,managedChannels]=await Promise.all([
      api("/admin/dashboard/summary"),
      api("/admin/operations/workers/whatsapp").catch(()=>({configured:false,worker_active:false,worker_lease_ttl_seconds:0,queued:0,processing:0,retrying:0,dead:0})),
      api("/admin/channels/managed/requests").catch(()=>({count:0,requests:[]}))
    ]);
    const workerState=!worker.configured?"Not configured":worker.worker_active?"Online":"Offline";
    const lifecycle=data.lifecycle_counts||{};
    const sourceCounts=data.source_counts||{};
    const cards=[
      ["Managed Delivery",adminNumber(sourceCounts.managed)],
      ["Self-Service",adminNumber(sourceCounts.self_service)],
      ["Onboarding",adminNumber(lifecycle.onboarding)],
      ["Testing",adminNumber(lifecycle.testing)],
      ["Live",adminNumber(lifecycle.live)],
      ["Paused",adminNumber(lifecycle.paused)],
      ["Suspended",adminNumber(lifecycle.suspended)],
      ["Runtime Active",adminNumber(data.active_companies)],
      ["Active Employees",`${adminNumber(data.active_agents)} / ${adminNumber(data.agents)}`],
      ["Active Channels",adminNumber(data.active_channels)],
      ["Active Services",adminNumber(data.active_subscriptions)],
      ["AI Requests · 24h",adminNumber(data.ai_requests_24h)],
      ["AI Failures · 24h",adminNumber(data.failed_ai_requests_24h)],
      ["External Ops Pending",adminNumber(data.unresolved_external_operations)],
      ["Managed Channel Requests",adminNumber(managedChannels.count)],
      ["WhatsApp Worker",workerState],
      ["WhatsApp Queue",`${adminNumber(worker.queued)} queued · ${adminNumber(worker.retrying)} retrying`],
      ["Provider Cost · 30d",adminMoney(data.provider_cost_30d)]
    ];
    document.getElementById("dashboard-cards").innerHTML=cards.map(([label,value])=>`<div class="card"><div class="card-label">${escapeAdmin(label)}</div><div class="card-value">${escapeAdmin(value)}</div></div>`).join("");
    const managedByCompany=new Map();
    for(const item of managedChannels.requests||[]){
      const companyId=Number(item.company_id||0);if(!companyId)continue;
      const existing=managedByCompany.get(companyId)||{company_id:companyId,company_name:item.company_name||`Company #${companyId}`,count:0};
      existing.count+=1;managedByCompany.set(companyId,existing);
    }
    data.attention_items=[
      ...(data.attention_items||[]),
      ...Array.from(managedByCompany.values()).map(item=>({
        ...item,
        type:"managed_channel_request",
        title:"Managed channel setup",
        severity:"review",
        tab:"channels"
      }))
    ];
    renderAdminAttention(data);
  }catch(e){
    console.error(e);
    const attention=document.getElementById("dashboard-attention");if(attention)attention.innerHTML=`<div class="error">${escapeAdmin(e.message)}</div>`;
  }
}

async function loadCompanies(){
  try{
    const data=await api("/admin/companies");companiesCache=data.companies||[];
    document.getElementById("companies-table").innerHTML=companiesCache.map(company=>`<tr><td>${company.id}</td><td><strong>${escapeAdmin(company.name)}</strong></td><td><span class="status ${adminLifecycleClass(company.lifecycle_status)}">${escapeAdmin(adminLifecycleLabel(company.lifecycle_status))}</span></td><td><span class="status ${company.active?'status-active':'status-inactive'}">${company.active?'Running':'Stopped'}</span></td><td>${company.created_at?new Date(company.created_at).toLocaleDateString():''}</td><td><button class="table-button" onclick="${xvondSupportMode()?`openSupportCompany(${Number(company.id)})`:`openCompany(${Number(company.id)})`}">${xvondSupportMode()?'Inspect':'Open'}</button></td></tr>`).join("")
  }catch(e){alert(e.message)}
}

async function openSupportCompany(companyId){
  if(!xvondSupportMode())return openCompany(companyId);
  try{
    const [view,usage,external,deliveries]=await Promise.all([
      api(`/admin/company-view/${companyId}`),
      api(`/admin/operations/companies/${companyId}/usage`),
      api(`/admin/operations/companies/${companyId}/external-unresolved`),
      api(`/admin/operations/whatsapp/deliveries/unresolved?company_id=${companyId}&limit=100`)
    ]);
    const company=view.company||{},summary=usage.summary||{},agents=view.agents||[],channels=view.channels||[],services=view.services||[],deliveryRows=deliveries.deliveries||[],operationRows=external.requests||[];
    document.querySelectorAll('.page').forEach(item=>item.classList.add('hidden'));
    document.getElementById('page-company-detail').classList.remove('hidden');document.getElementById('page-title').textContent=company.name||`Company #${companyId}`;
    document.getElementById('company-detail').innerHTML=`<div class="workspace-shell"><div class="workspace-hero"><div><div class="workspace-eyebrow">Support · Read-only Operations</div><h2>${escapeAdmin(company.name||`Company #${companyId}`)}</h2><div class="workspace-subtitle">No tenant message bodies or mutation controls are exposed in this view.</div></div><div class="workspace-hero-actions"><span class="status ${adminLifecycleClass(company.lifecycle_status)}">${escapeAdmin(adminLifecycleLabel(company.lifecycle_status))}</span><span class="status ${company.active?'status-active':'status-inactive'}">Runtime ${company.active?'Running':'Stopped'}</span></div></div><div class="workspace-metrics"><div class="metric-card"><span>AI Employees</span><strong>${adminNumber(agents.length)}</strong><small>${adminNumber(agents.filter(x=>x.enabled).length)} live</small></div><div class="metric-card"><span>Channels</span><strong>${adminNumber(channels.length)}</strong><small>${adminNumber(channels.filter(x=>x.enabled).length)} active</small></div><div class="metric-card"><span>Active Services</span><strong>${adminNumber(services.filter(x=>x.status==='active').length)}</strong></div><div class="metric-card"><span>AI Requests</span><strong>${adminNumber(summary.requests)}</strong></div><div class="metric-card"><span>Total Tokens</span><strong>${adminNumber(summary.total_tokens)}</strong></div><div class="metric-card"><span>Provider Cost</span><strong>${adminMoney(summary.provider_cost)}</strong></div></div><div class="workspace-grid two-col"><div class="workspace-panel"><div class="workspace-panel-head"><div><h3>External Operations</h3><p>Metadata requiring admin reconciliation.</p></div></div>${operationRows.length?`<div class="info-stack">${operationRows.map(x=>`<div><span>${escapeAdmin(x.action_type||'operation')} #${Number(x.id)}</span><strong>${escapeAdmin(x.status||'unknown')}</strong></div>`).join('')}</div>`:'<div class="workspace-empty"><strong>No unresolved external operations</strong></div>'}</div><div class="workspace-panel"><div class="workspace-panel-head"><div><h3>WhatsApp Deliveries</h3><p>Transport metadata only; customer content is not exposed.</p></div></div>${deliveryRows.length?`<div class="info-stack">${deliveryRows.map(x=>`<div><span>Delivery #${Number(x.id)} · conversation #${Number(x.conversation_id||0)}</span><strong>${escapeAdmin(x.status||'unknown')} · attempts ${Number(x.attempts||0)}</strong></div>`).join('')}</div>`:'<div class="workspace-empty"><strong>No unresolved WhatsApp deliveries</strong></div>'}</div></div><div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Employees & Channels</h3><p>Operational identity only.</p></div></div><div class="info-stack">${agents.map(agent=>`<div><span>${escapeAdmin(agent.name)} · ${escapeAdmin(agent.provider||'')} ${escapeAdmin(agent.model||'')}</span><strong>${agent.enabled?'Live':'Draft/Paused'}</strong></div>`).join('')||'<div>No AI employees</div>'}${channels.map(channel=>`<div><span>${escapeAdmin(channel.type)} · employee #${Number(channel.agent_id||0)}</span><strong>${channel.enabled?'Active':'Inactive'}</strong></div>`).join('')}</div></div></div>`;
  }catch(error){alert(error.message)}
}

function openCreateCompany(){if(xvondSupportMode()){alert('Support access is read-only.');return}openModal("Create Company",`<div class="form-group"><label>Company Name</label><input id="company-name"></div><div class="form-group"><label>Owner Full Name</label><input id="owner-name"></div><div class="form-group"><label>Owner Email</label><input id="owner-email" type="email"></div><div class="form-group"><label>Owner Password</label><input id="owner-password" type="password"></div><p class="meta">The company starts in Onboarding. Customer portal access is available while AI runtime remains stopped until Go Live.</p><button class="modal-submit" onclick="createCompany()">Create Company</button>`)}
async function createCompany(){if(xvondSupportMode()){alert('Support access is read-only.');return}try{const data=await api("/admin/companies",{method:"POST",body:JSON.stringify({name:document.getElementById("company-name").value.trim(),owner_full_name:document.getElementById("owner-name").value.trim(),owner_email:document.getElementById("owner-email").value.trim(),owner_password:document.getElementById("owner-password").value})});closeModal();await loadCompanies();await openCompany(data.company.id)}catch(e){alert(e.message)}}

function openAddAIEmployee(companyId){if(xvondSupportMode()){alert('Support access is read-only.');return}simpleCompanyId=Number(companyId);openModal("Create AI Employee",employeeForm({},false));const language=document.getElementById("simple-language"),style=document.getElementById("simple-conversation-style");if(language)language.value="auto";if(style)style.value="professional_friendly"}

function openModal(title,body){document.getElementById("modal-title").textContent=title;document.getElementById("modal-body").innerHTML=body;document.getElementById("modal").classList.remove("hidden")}
function closeModal(){document.getElementById("modal").classList.add("hidden")}

function openAgentTestChat(companyId,agentId){if(xvondSupportMode()){alert('Support access is read-only.');return}agentTestConversationId=null;openModal("Test AI Employee",`<div id="agent-test-transcript" class="agent-test-transcript"><p class="meta">Internal test conversation. Real provider usage is recorded.</p></div><div class="form-group"><label>Message</label><textarea id="agent-test-message" placeholder="Type a message..."></textarea></div><button id="agent-test-send" class="modal-submit" onclick="sendAgentTestMessage(${companyId},${agentId})">Send Message</button>`)}
async function sendAgentTestMessage(companyId,agentId){
  if(xvondSupportMode()){alert('Support access is read-only.');return}
  const input=document.getElementById("agent-test-message"),button=document.getElementById("agent-test-send"),transcript=document.getElementById("agent-test-transcript"),message=input.value.trim();if(!message){alert("Message is required.");return}button.disabled=true;button.textContent="Sending...";
  try{const result=await api(`/admin/companies/${companyId}/agents/${agentId}/test-chat`,{method:"POST",body:JSON.stringify({message,conversation_id:agentTestConversationId})});agentTestConversationId=result.conversation_id;transcript.innerHTML+=`<div class="test-message test-user"><strong>You</strong><div>${escapeAdmin(message)}</div></div><div class="test-message test-assistant"><strong>AI Employee</strong><div>${escapeAdmin(result.response?.content||"")}</div><small>${Number(result.usage?.total_tokens||0)} tokens · ${Number(result.usage?.latency_ms||0)} ms</small></div>`;input.value="";transcript.scrollTop=transcript.scrollHeight}catch(error){transcript.innerHTML+=`<div class="test-message test-error"><strong>Error</strong><div>${escapeAdmin(error.message)}</div></div>`}finally{button.disabled=false;button.textContent="Send Message"}
}

resumeAdminSession();