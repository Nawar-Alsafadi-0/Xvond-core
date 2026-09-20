const XVOND_BUSINESS_CAPABILITY_INFO={
  booking:{label:'Booking & Reservations',description:'Appointments, reservations, availability, rescheduling and cancellation.'},
  orders:{label:'Orders & Requests',description:'Customer orders, service requests and fulfillment workflows.'},
  quotation:{label:'Quotation',description:'Quote requests and pricing/quotation workflows.'},
  lead_management:{label:'Lead Management',description:'Lead capture, qualification and follow-up.'},
  customer_support:{label:'Customer Support',description:'Support requests, complaints, callbacks and service cases.'}
};
const XVOND_EXECUTABLE_INTEGRATIONS=new Set(['webhook','custom_api','pos','crm','erp']);
let xvondWorkspace={companyId:null,tab:'overview',data:null};
let xvondIntegrationDraft=null;

function wsDate(v){if(!v)return '—';try{return new Date(v).toLocaleString()}catch(_e){return String(v)}}
function wsMoney(v){const n=Number(v||0);return Number.isFinite(n)?n.toFixed(3):'0.000'}
function wsLines(values){return (values||[]).map(x=>typeof x==='string'?x:JSON.stringify(x)).join('\n')}
function wsParseLines(value){return String(value||'').split('\n').map(x=>x.trim()).filter(Boolean).map(x=>{if(x.startsWith('{')){try{const item=JSON.parse(x);if(item&&typeof item==='object'&&!Array.isArray(item))return item}catch(_e){throw new Error('A structured business fact contains invalid JSON')}}return x})}
function wsAgentName(id){return (xvondWorkspace.data?.view?.agents||[]).find(x=>+x.id===+id)?.name||`AI Employee #${id}`}
function wsModuleMap(){return new Map((xvondWorkspace.data?.modules||[]).map(x=>[x.module_name,x]))}
function wsEnabledBusinessModules(){const map=wsModuleMap();return Object.keys(XVOND_BUSINESS_CAPABILITY_INFO).filter(name=>map.get(name)?.enabled)}
function wsChannel(agentId,type){return (xvondWorkspace.data?.channels||[]).find(x=>+x.agent_id===+agentId&&x.channel_type===type)}
function wsChannelPresentation(channel){
  if(!channel)return {label:'Not connected',kind:'bad'};
  if(channel.channel_type==='whatsapp'){
    if(channel.connected===true){
      if(!channel.enabled)return {label:'Connected · Inactive',kind:'neutral'};
      return {label:channel.coexistence?'Connected · Coexistence':'Connected',kind:'good'};
    }
    if(channel.connection_status==='invalid_token')return {label:'Disconnected · Invalid token',kind:'bad'};
    if(['meta_rejected','phone_mismatch','invalid_configuration'].includes(channel.connection_status))return {label:'Disconnected',kind:'bad'};
    if(channel.connection_status==='check_failed')return {label:'Connection check failed',kind:'bad'};
    if(channel.configured)return {label:'Configured only',kind:'neutral'};
    return {label:'Needs setup',kind:'bad'};
  }
  if(channel.enabled&&channel.connected===true)return {label:'Active',kind:'good'};
  if(channel.configured)return {label:'Configured',kind:'neutral'};
  return {label:'Needs setup',kind:'bad'};
}
function wsChannelDetail(channel){
  if(!channel)return 'No channel has been created.';
  if(channel.channel_type!=='whatsapp')return channel.enabled?'Serving customer traffic.':'Not serving customer traffic.';
  const local=channel.enabled?'Local channel active':'Local channel inactive';
  if(channel.connected===true){
    return `${local} · Meta verified${channel.coexistence?' · WhatsApp Business App coexistence':''}`;
  }
  return `${local} · ${channel.connection_issue||'WhatsApp is not connected to Meta.'}`;
}
function wsManagedChannelRequests(agentId){
  return (xvondWorkspace.data?.channels||[]).filter(channel=>{
    if(+channel.agent_id!==+agentId)return false;
    if(['website','whatsapp','voice'].includes(String(channel.channel_type||'')))return false;
    return channel.setup_mode==='managed';
  });
}
function wsManagedChannelPresentation(channel){
  const state=String(channel?.config?.provisioning_state||'').toLowerCase();
  if(channel?.enabled&&channel?.customer_roundtrip_verified===true)return {label:'Live · Round-trip verified',kind:'good'};
  if(channel?.enabled)return {label:'Active · Awaiting round-trip',kind:'neutral'};
  if(state==='cancelled')return {label:'Cancelled',kind:'neutral'};
  if(channel?.runtime_state!=='live')return {label:'Adapter required',kind:'bad'};
  if(state==='connected')return {label:'Provider configured',kind:'neutral'};
  if(state==='requested')return {label:'Requested by Job Brief',kind:'neutral'};
  return {label:'Xvond setup',kind:'neutral'};
}
function wsManagedChannelDetail(channel){
  const source=String(channel?.config?.request_source||'').replaceAll('_',' ');
  const callback=String(channel?.config?.provider_inbound_url||'').trim();
  if(channel?.runtime_state!=='live'){
    return `Requested${source?` via ${source}`:''}. Keep disabled until Xvond ships and validates the runtime adapter.`;
  }
  if(callback)return `Provider credentials are secured and the Xvond route is configured. Complete the provider callback with: ${callback}`;
  return `Xvond-managed provisioning${source?` requested via ${source}`:''}. Do not activate until provider/runtime verification is complete.`;
}
function wsManagedChannelActions(channel){
  if(xvondSupportMode()||channel?.runtime_state!=='live'||channel?.enabled)return '';
  const state=String(channel?.config?.provisioning_state||'').toLowerCase();
  const agent=(xvondWorkspace.data?.view?.agents||[]).find(item=>+item.id===+channel.agent_id);
  const company=xvondWorkspace.data?.view?.company||{};
  const setupLabel=state==='connected'?'Review / Update Setup':'Complete Setup';
  const activate=(state==='connected'&&company.active===true&&agent?.enabled===true)
    ? `<button class="primary-button" onclick="activateManagedChannel(${Number(channel.id)})">Activate Channel</button>`
    : '';
  return `<div class="employee-actions" style="margin-top:12px"><button onclick="openManagedChannelSetup(${Number(channel.id)})">${setupLabel}</button>${activate}</div>`;
}
function renderManagedChannelRequests(agentId){
  const items=wsManagedChannelRequests(agentId);
  if(!items.length)return '';
  return `<div class="workspace-panel" style="margin-top:14px"><div class="workspace-panel-head"><div><h4>Managed Channel Requests</h4><p>Each channel uses its provider-specific secure setup. Configured means the encrypted route exists; Live means it passed all activation checks.</p></div></div><div class="channel-grid">${items.map(channel=>{
    const state=wsManagedChannelPresentation(channel);
    return `<div class="channel-card"><div><span class="channel-name">${f(channel.channel_name||channel.channel_type)}</span>${wsPill(state.label,state.kind)}</div><p>${f(wsManagedChannelDetail(channel))}</p><div class="meta">Runtime: ${f(channel.runtime_state||'unknown')} · Setup: Xvond managed · Local: ${channel.enabled?'Active':'Inactive'}</div>${wsManagedChannelActions(channel)}</div>`;
  }).join('')}</div></div>`;
}

window.openManagedChannelSetup=async function(channelId){
  const channel=(xvondWorkspace.data?.channels||[]).find(item=>+item.id===+channelId);
  if(!channel){alert('Managed channel request not found.');return}
  try{
    const catalog=await api('/admin/channels/catalog');
    const definition=(catalog.channels||[]).find(item=>item.type===channel.channel_type)||{};
    const setup=definition.provider_setup||{};
    xvondWorkspace.managedProviderSetup={channelId:Number(channel.id),setup};
    const cfg=channel.config||{};
    const fields=(setup.fields||[]).map(field=>{
      const id=`managed-provider-${String(field.name||'').replaceAll('_','-')}`;
      const type=field.secret?'password':(field.input_type||'text');
      const placeholder=field.placeholder!=null?String(field.placeholder):(field.default!=null?String(field.default):'');
      const help=field.help?`<small style="display:block;margin-top:6px;color:var(--muted)">${f(field.help)}</small>`:'';
      return `<div class="form-group"><label>${f(field.label||field.name)}${field.required?' *':''}</label><input id="${f(id)}" data-provider-field="${f(field.name)}" type="${f(type)}" autocomplete="${field.secret?'new-password':'off'}" placeholder="${f(placeholder)}">${help}</div>`;
    }).join('');
    const connected=String(cfg.provisioning_state||'').toLowerCase()==='connected';
    const callback=String(cfg.provider_inbound_url||'').trim();
    const callbackBlock=callback
      ?`<div class="modal-intro"><strong>Provider callback / webhook</strong><p style="overflow-wrap:anywhere">${f(callback)}</p>${setup.callback_note?`<p>${f(setup.callback_note)}</p>`:''}<button type="button" onclick="copyManagedChannelCallback(${Number(channel.id)})">Copy callback URL</button></div>`
      :'';
    openModal(
      `${connected?'Manage':'Connect'} ${f(channel.channel_name||channel.channel_type)}`,
      `<div class="modal-intro"><strong>${f(setup.provider_name||channel.channel_name||channel.channel_type)}</strong><p>${f(setup.setup_note||'Connect the provider account used for this channel.')}</p><p>Secrets are sent once to the encrypted workflow registry. They are never stored in Core and are not shown again.</p></div>
        ${callbackBlock}
        <div class="form-group"><label>Connected Account Label</label><input id="managed-channel-label" value="${f(cfg.provider_account_label||'')}" placeholder="${f(setup.account_label_placeholder||'e.g. Customer Support Account')}"></div>
        ${fields}
        <div class="form-group"><label>Channel-only Instructions</label><textarea id="managed-channel-instructions" placeholder="Optional transport/channel rules only">${f(cfg.channel_instructions||'')}</textarea></div>
        <input id="managed-channel-key" type="hidden" value="${f(cfg.connection_key||'')}">
        <button class="modal-submit" onclick="saveManagedChannelSetup(${Number(channel.id)})">${connected?'Verify / Save Changes':'Securely Configure Provider'}</button>`
    );
  }catch(error){alert(error.message)}
};

window.copyManagedChannelCallback=async function(channelId){
  const channel=(xvondWorkspace.data?.channels||[]).find(item=>+item.id===+channelId);
  const callback=String(channel?.config?.provider_inbound_url||'').trim();
  if(!callback){alert('No provider callback URL is available yet.');return}
  try{await navigator.clipboard.writeText(callback)}
  catch(_error){prompt('Copy the provider callback URL:',callback)}
};

window.saveManagedChannelSetup=async function(channelId){
  try{
    const channel=(xvondWorkspace.data?.channels||[]).find(item=>+item.id===+channelId);
    if(!channel)throw new Error('Managed channel request not found.');
    const setup=xvondWorkspace.managedProviderSetup?.channelId===Number(channelId)
      ?(xvondWorkspace.managedProviderSetup.setup||{})
      :{};
    const connection_key=String(document.getElementById('managed-channel-key')?.value||'').trim()||null;
    const provider_account_label=String(document.getElementById('managed-channel-label')?.value||'').trim()||null;
    const channel_instructions=String(document.getElementById('managed-channel-instructions')?.value||'').trim()||null;
    const provider_config={};
    let hasProviderValue=false;
    for(const field of setup.fields||[]){
      const id=`managed-provider-${String(field.name||'').replaceAll('_','-')}`;
      let value=String(document.getElementById(id)?.value||'').trim();
      if(!value&&field.default!=null)value=String(field.default);
      if(value){provider_config[field.name]=value;hasProviderValue=true}
      if(field.required&&!value&&String(channel?.config?.provisioning_state||'').toLowerCase()!=='connected'){
        throw new Error(`${field.label||field.name} is required.`);
      }
    }
    const body={connection_key,provider_account_label,channel_instructions};
    if(hasProviderValue||String(channel?.config?.provisioning_state||'').toLowerCase()!=='connected'){
      body.provider_config=provider_config;
    }
    await api(`/admin/channels/${Number(channelId)}/managed-connect`,{
      method:'POST',
      body:JSON.stringify(body)
    });
    closeModal();
    await loadCompanyControlCenter(xvondWorkspace.companyId,'channels');
  }catch(error){alert(error.message)}
};

window.activateManagedChannel=async function(channelId){
  try{
    await api(`/admin/channels/${Number(channelId)}`,{
      method:'PUT',
      body:JSON.stringify({enabled:true})
    });
    await loadCompanyControlCenter(xvondWorkspace.companyId,'channels');
  }catch(error){alert(error.message)}
};
function wsActiveOperations(){return (xvondWorkspace.data?.requests||[]).filter(x=>!['completed','cancelled'].includes(x.status))}
function wsEnabledOperationCount(moduleName){return (xvondWorkspace.data?.agentMeta||[]).reduce((sum,row)=>sum+(row.actions||[]).filter(x=>x.enabled===true&&x.module===moduleName).length,0)}
function wsPill(text,kind='neutral'){return `<span class="workspace-pill ${kind}">${f(text)}</span>`}
function wsEmpty(title,body=''){return `<div class="workspace-empty"><strong>${f(title)}</strong>${body?`<p>${f(body)}</p>`:''}</div>`}
function wsOption(value,selected,label=null){return `<option value="${f(value)}" ${String(value)===String(selected||'')?'selected':''}>${f(label||value)}</option>`}
function wsSelect(values,selected,empty='Select'){return `<option value="">${f(empty)}</option>`+(values||[]).map(x=>wsOption(x,selected)).join('')}
async function wsOptional(path,fallback,label=null,issues=null,timeoutMs=8000){
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),timeoutMs);
  try{return await api(path,{signal:controller.signal})}
  catch(error){
    const message=error?.name==='AbortError'?'Timed out':(error?.message||"Unavailable");
    if(Array.isArray(issues)&&label)issues.push({label,message});
    return fallback;
  }
  finally{clearTimeout(timer)}
}

async function loadCompanyControlCenter(companyId,tab=null){
  simpleCompanyId=Number(companyId);
  xvondWorkspace.companyId=Number(companyId);
  if(tab)xvondWorkspace.tab=tab;
  const loadIssues=[];

  const viewController=new AbortController();
  const viewTimer=setTimeout(()=>viewController.abort(),8000);
  let view;
  try{view=await api(`/admin/company-view/${companyId}`,{signal:viewController.signal})}
  finally{clearTimeout(viewTimer)}

  const [channelResult,moduleResult,profile,readiness,serviceBilling]=await Promise.all([
    wsOptional(`/admin/channels/companies/${companyId}`,{channels:[]},'Channels',loadIssues,6000),
    wsOptional(`/admin/companies/${companyId}/modules`,{modules:[]},'Company capabilities',loadIssues,6000),
    wsOptional(`/admin/company-profile/${companyId}`,{company_name:view.company?.name||''},'Company profile',loadIssues,6000),
    wsOptional(`/admin/production/companies/${companyId}/readiness`,null,'Managed delivery readiness',loadIssues,6000),
    wsOptional(`/admin/service-billing/companies/${companyId}`,{services:[]},'Billing',loadIssues,6000)
  ]);

  xvondWorkspace.data={
    view,
    channels:channelResult.channels||[],
    modules:moduleResult.modules||[],
    catalog:{templates:[]},
    integrations:[],
    requests:[],
    conversations:[],
    usage:{summary:{},usage:[]},
    profile,
    setup:{},
    handoffs:[],
    audit:[],
    billingServices:serviceBilling.services||[],
    plans:[],
    users:view.users||[],
    readiness,
    unresolved:[],
    agentMeta:(view.agents||[]).map(agent=>({agent,profile:{name:agent.name},knowledge:[],actions:[],operationsReady:false})),
    loadIssues,
    loadedTabs:new Set(['overview'])
  };
  renderCompanyControlCenter();
  if(xvondWorkspace.tab!=='overview') await hydrateWorkspaceTab(xvondWorkspace.tab);
}

async function hydrateWorkspaceTab(tab){
  const d=xvondWorkspace.data;
  if(!d||d.loadedTabs?.has(tab))return;
  const companyId=xvondWorkspace.companyId;
  const issues=d.loadIssues||[];
  try{
    if(tab==='agents'||tab==='knowledge'||tab==='operations'){
      const rows=await Promise.all((d.view.agents||[]).map(async agent=>{
        const [agentProfile,knowledge,actions]=await Promise.all([
          wsOptional(`/admin/ai-employee-profile/companies/${companyId}/${agent.id}`,{name:agent.name},`${agent.name} profile`,issues,6000),
          wsOptional(`/admin/ai-employees/companies/${companyId}/${agent.id}/knowledge`,{items:[]},`${agent.name} knowledge`,issues,6000),
          wsOptional(`/admin/agent-actions/${agent.id}`,{actions:[],ready:false},`${agent.name} actions`,issues,6000)
        ]);
        return {agent,profile:agentProfile,knowledge:knowledge.items||[],actions:actions.actions||[],operationsReady:!!actions.ready};
      }));
      d.agentMeta=rows;
    }
    if(tab==='integrations'){
      const result=await wsOptional(`/admin/integrations/companies/${companyId}`,{integrations:[]},'Integrations',issues,6000);
      d.integrations=result.integrations||[];
    }
    if(tab==='operations'){
      const [requests,unresolved,catalog]=await Promise.all([
        wsOptional(`/admin/agent-actions/companies/${companyId}/requests`,{requests:[]},'Operations',issues,6000),
        wsOptional(`/admin/operations/companies/${companyId}/external-unresolved`,{requests:[]},'External reconciliation',issues,6000),
        wsOptional('/admin/agent-actions/templates/catalog',{templates:[]},'Action catalog',issues,6000)
      ]);
      d.requests=requests.requests||[];
      d.unresolved=unresolved.requests||[];
      d.catalog=catalog;
    }
    if(tab==='conversations'){
      const [conversations,handoffs]=await Promise.all([
        wsOptional(`/admin/operations/companies/${companyId}/conversations`,{conversations:[]},'Conversations',issues,6000),
        wsOptional(`/admin/handoff/companies/${companyId}/sessions`,{sessions:[]},'Handoff sessions',issues,6000)
      ]);
      d.conversations=conversations.conversations||[];
      d.handoffs=handoffs.sessions||[];
    }
    if(tab==='usage'){
      d.usage=await wsOptional(`/admin/operations/companies/${companyId}/usage`,{summary:{},usage:[]},'Usage',issues,6000);
    }
    if(tab==='billing'){
      const plans=await wsOptional('/admin/service-billing/plans',{plans:[]},'Plan catalog',issues,6000);
      d.plans=plans.plans||[];
    }
    if(tab==='users'){
      const users=await wsOptional(`/admin/company-users/companies/${companyId}`,{users:[]},'Company users',issues,6000);
      d.users=(users.users&&users.users.length?users.users:d.view.users)||[];
    }
    if(tab==='logs'){
      const audit=await wsOptional(`/admin/audit/?company_id=${companyId}&limit=100`,{logs:[],total:0},'Audit trail',issues,6000);
      d.audit=audit.logs||[];
    }
    if(tab==='company'){
      d.setup=await wsOptional('/admin/setup/catalog',{},'Setup catalog',issues,6000);
    }
  }finally{
    d.loadedTabs?.add(tab);
    renderCompanyControlCenter();
  }
}
function renderCompanyControlCenter(){
  const d=xvondWorkspace.data;if(!d)return;const c=d.view.company,p=d.profile||{};
  const selfService=String(c.onboarding_source||'managed')==='self_service';
  document.querySelectorAll('.page').forEach(x=>x.classList.add('hidden'));
  document.getElementById('page-company-detail').classList.remove('hidden');
  document.getElementById('page-title').textContent=c.name;
  const tabs=selfService
    ?[['overview','Overview'],['company','Workspace'],['agents','AI Employees'],['knowledge','Knowledge'],['channels','Connections'],['operations','Operations'],['integrations','Integrations'],['usage','Usage'],['users','Users'],['billing','Billing'],['logs','Audit']]
    :[['overview','Overview'],['company','Company'],['capabilities','Capabilities'],['agents','AI Employees'],['knowledge','Knowledge'],['channels','Channels'],['operations','Operations'],['integrations','Integrations'],['conversations','Conversations'],['usage','Usage'],['users','Users'],['billing','Billing'],['logs','Audit']];
  const sourceLabel=selfService?'Self-Service Workspace':'Managed Delivery Workspace';
  const sourcePill=wsPill(selfService?'Self-Service':'Managed by Xvond',selfService?'good':'neutral');
  const managedActions=selfService?'':`<button class="primary-button" onclick="openCompanyIdentityEditor()">Edit Company</button>`;
  document.getElementById('company-detail').innerHTML=`<div class="workspace-shell"><div class="workspace-hero"><div><div class="workspace-eyebrow">${sourceLabel}</div><h2>${f(c.name)}</h2><div class="workspace-subtitle">${f(p.business_type||'Business type not set')}${p.country?` · ${f(p.country)}`:''}</div></div><div class="workspace-hero-actions">${sourcePill}${wsPill(c.active?'Runtime Active':'Runtime Stopped',c.active?'good':'bad')}${managedActions}</div></div><div class="workspace-tabs">${tabs.map(([key,label])=>`<button class="workspace-tab ${xvondWorkspace.tab===key?'active':''}" onclick="switchWorkspaceTab('${key}')">${label}</button>`).join('')}</div><div id="workspace-content">${renderWorkspaceTab()}</div></div>`;
}
async function switchWorkspaceTab(tab){xvondWorkspace.tab=tab;renderCompanyControlCenter();await hydrateWorkspaceTab(tab)}
function renderWorkspaceTab(){switch(xvondWorkspace.tab){case'company':return renderCompanyProfileTab();case'capabilities':return renderCapabilitiesTab();case'agents':return renderAgentsTab();case'knowledge':return renderKnowledgeTab();case'channels':return renderChannelsTab();case'operations':return renderOperationsTab();case'integrations':return renderIntegrationsTab();case'conversations':return renderConversationsTab();case'usage':return renderUsageTab();case'users':return renderUsersTab();case'billing':return renderBillingTab();case'logs':return renderLogsTab();default:return renderOverviewTab()}}

function renderOverviewTab(){
  const d=xvondWorkspace.data,a=d.view.analytics||{},r=d.readiness;
  const connectedChannels=d.channels.filter(x=>x.enabled===true&&x.connected===true);
  const checks=r?[['Company profile',!!r.profile_ready],['AI service subscription',!!r.subscription_ready],['Production-ready AI employee',(r.agents||[]).some(x=>x.ready)],['Connected channel',connectedChannels.length>0]]:[['Company profile',!!d.profile.business_type&&!!d.profile.country],['AI employee',d.view.agents.length>0],['Business knowledge',d.agentMeta.some(x=>x.knowledge.some(k=>k.enabled))],['Connected channel',connectedChannels.length>0]];
  return `<div class="workspace-metrics"><div class="metric-card"><span>AI Employees</span><strong>${d.view.agents.length}</strong><small>${d.view.agents.filter(x=>x.enabled).length} active</small></div><div class="metric-card"><span>Capabilities</span><strong>${wsEnabledBusinessModules().length}</strong><small>business modules enabled</small></div><div class="metric-card"><span>Channels</span><strong>${connectedChannels.length}</strong><small>${connectedChannels.length} connected · ${d.channels.filter(x=>x.configured).length} configured · ${d.channels.length} created</small></div><div class="metric-card"><span>Open Operations</span><strong>${wsActiveOperations().length}</strong><small>${d.unresolved.length} external unresolved</small></div><div class="metric-card"><span>Conversations</span><strong>${a.conversations||0}</strong></div><div class="metric-card"><span>AI Requests</span><strong>${a.ai_requests||0}</strong><small>cost ${wsMoney(a.provider_cost)}</small></div></div><div class="workspace-grid two-col"><div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Production Readiness</h3><p>${f(r?.status||'Configuration checks')}</p></div></div><div class="readiness-list">${checks.map(([label,ok])=>`<div class="readiness-row"><span class="readiness-dot ${ok?'ok':'missing'}"></span><span>${f(label)}</span><strong>${ok?'Ready':'Needs setup'}</strong></div>`).join('')}</div>${(r?.issues||[]).length?`<div class="workspace-empty"><strong>Blockers</strong><p>${(r.issues||[]).map(f).join(' · ')}</p></div>`:''}</div><div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Company Snapshot</h3><p>Single source of company identity.</p></div><button class="table-button" onclick="openCompanyIdentityEditor()">Edit</button></div><div class="info-grid"><div><span>Business type</span><strong>${f(d.profile.business_type||'—')}</strong></div><div><span>Country</span><strong>${f(d.profile.country||'—')}</strong></div><div><span>Currency</span><strong>${f(d.profile.currency||'—')}</strong></div><div><span>Timezone</span><strong>${f(d.profile.timezone||'—')}</strong></div><div><span>Language</span><strong>${f(d.profile.primary_language||'—')}</strong></div><div><span>Active services</span><strong>${d.billingServices.filter(x=>x.status==='active').length}</strong></div></div></div></div><div class="workspace-panel"><div class="architecture-flow"><span>Company</span><b>→</b><span>Capabilities</span><b>→</b><span>AI Employee</span><b>→</b><span>Knowledge + Actions</span><b>→</b><span>Channels</span><b>→</b><span>Customers</span></div></div>`;
}

function renderCompanyProfileTab(){const p=xvondWorkspace.data.profile;return `<div class="workspace-grid two-col"><div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Company Identity</h3><p>Used by every AI employee and channel.</p></div><button class="primary-button" onclick="openCompanyIdentityEditor()">Edit Identity</button></div><div class="info-grid"><div><span>Name</span><strong>${f(p.company_name)}</strong></div><div><span>Type</span><strong>${f(p.business_type||'—')}</strong></div><div><span>Country</span><strong>${f(p.country||'—')}</strong></div><div><span>Currency</span><strong>${f(p.currency||'—')}</strong></div><div><span>Timezone</span><strong>${f(p.timezone||'—')}</strong></div><div><span>Primary language</span><strong>${f(p.primary_language||'—')}</strong></div><div><span>Phone</span><strong>${f(p.phone||'—')}</strong></div><div><span>Email</span><strong>${f(p.email||'—')}</strong></div><div class="span-2"><span>Website</span><strong>${f(p.website||'—')}</strong></div><div class="span-2"><span>Description</span><strong>${f(p.description||'—')}</strong></div></div></div><div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Business Information</h3><p>Saved once and synchronized to all AI employees.</p></div><button class="primary-button" onclick="openBusinessInformationEditor()">Edit Business Info</button></div><div class="info-stack"><div><span>Services</span><strong>${(p.services||[]).length || 'No services saved'}</strong></div><div><span>Locations / branches</span><strong>${(p.locations||[]).length}</strong></div><div><span>Service areas</span><strong>${(p.service_areas||[]).length}</strong></div><div><span>Policies</span><strong>${(p.policies||[]).length}</strong></div><div><span>Business rules</span><strong>${(p.business_rules||[]).length}</strong></div><div><span>Working days configured</span><strong>${Object.values(p.working_hours||{}).filter(x=>x&&x.enabled!==false).length}</strong></div></div></div></div>`}
function renderCapabilitiesTab(){const map=wsModuleMap();return `<div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Business Capabilities</h3><p>Enable only what this client actually needs.</p></div></div><div class="capability-grid">${Object.entries(XVOND_BUSINESS_CAPABILITY_INFO).map(([name,info])=>{const row=map.get(name),enabled=!!row?.enabled,used=wsEnabledOperationCount(name);return `<div class="capability-card ${enabled?'enabled':''}"><div class="capability-icon">${enabled?'✓':'+'}</div><div><h4>${f(info.label)}</h4><p>${f(info.description)}</p><div class="meta">${used} enabled action${used===1?'':'s'} using this capability</div></div><button class="${enabled?'table-button':'primary-button'}" onclick="toggleWorkspaceCapability('${name}',${!enabled})">${enabled?'Disable':'Enable'}</button></div>`}).join('')}</div></div>`}

function renderAgentsTab(){const d=xvondWorkspace.data;return `<div class="workspace-panel"><div class="workspace-panel-head"><div><h3>AI Employees</h3><p>Information → Knowledge → Actions → Channels → Conversations.</p></div><button class="primary-button" onclick="openAddAIEmployee(${d.view.company.id})">+ AI Employee</button></div>${d.agentMeta.length?`<div class="employee-grid">${d.agentMeta.map(row=>{const a=row.agent,channels=d.channels.filter(x=>+x.agent_id===+a.id),live=channels.some(x=>x.enabled),readyActions=row.actions.filter(x=>x.enabled===true&&!(x.readiness_issues||[]).length).length;return `<div class="employee-card"><div class="employee-card-top"><div class="employee-avatar">${f((a.name||'A').slice(0,1).toUpperCase())}</div><div><h4>${f(a.name)}</h4><div class="meta">${a.enabled?'Active AI Employee':'Paused AI Employee'}</div></div>${wsPill(a.enabled?'Active':'Paused',a.enabled?'good':'neutral')}</div><div class="employee-stats"><div><strong>${row.knowledge.filter(x=>x.enabled).length}</strong><span>Knowledge</span></div><div><strong>${readyActions}</strong><span>Ready Actions</span></div><div><strong>${channels.length}</strong><span>Channels</span></div></div><div class="employee-flow-label">Information → Knowledge → Actions → Channels → Conversations</div><div class="employee-actions employee-primary-actions"><button onclick="openEditAIEmployee(${d.view.company.id},${a.id})">Information</button><button onclick="openKnowledgeManager(${d.view.company.id},${a.id})">Knowledge</button><button onclick="openAgentActions(${d.view.company.id},${a.id})">Actions</button><button onclick="switchWorkspaceTab('channels')">Channels</button><button onclick="openHumanTakeover(${d.view.company.id},${a.id})">Conversations</button></div><div class="employee-actions employee-secondary-actions"><button class="table-button" onclick="openAgentTestChat(${d.view.company.id},${a.id})">Test Employee</button><button class="danger" onclick="deleteWorkspaceEmployee(${a.id},${live})">Delete</button></div></div>`}).join('')}</div>`:wsEmpty('No AI employees yet','Create the employee core first. Company facts live in Company; channels and actions are connected separately.')}</div>`}
function renderKnowledgeTab(){const d=xvondWorkspace.data;return `<div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Knowledge</h3><p>Business Information is synchronized automatically. Add only extra sources here.</p></div></div>${d.agentMeta.length?`<div class="knowledge-agent-list">${d.agentMeta.map(row=>{const enabled=row.knowledge.filter(x=>x.enabled);return `<div class="knowledge-agent-row"><div><strong>${f(row.agent.name)}</strong><div class="meta">${enabled.length} active source${enabled.length===1?'':'s'} · ${enabled.reduce((n,x)=>n+Number(x.characters||0),0)} characters</div><div class="knowledge-chips">${enabled.slice(0,6).map(x=>`<span>${f(x.title)}</span>`).join('')}${enabled.length>6?`<span>+${enabled.length-6}</span>`:''}</div></div><button class="primary-button" onclick="openKnowledgeManager(${d.view.company.id},${row.agent.id})">Manage Knowledge</button></div>`}).join('')}</div>`:wsEmpty('Create an AI employee first')}</div>`}
window.openAddWorkspaceChannel=async function(){
  if(xvondSupportMode()){alert('Support access is read-only.');return}
  const agents=xvondWorkspace.data?.agentMeta||[];
  if(!agents.length){alert('Create an AI employee first.');return}
  try{
    const result=await api('/admin/channels/catalog');
    const channels=(result.channels||[]).filter(item=>item.runtime_state==='live');
    const existing=new Set((xvondWorkspace.data?.channels||[]).map(item=>`${item.agent_id}:${item.channel_type}`));
    const agentOptions=agents.map(row=>`<option value="${Number(row.agent.id)}">${f(row.agent.name)}</option>`).join('');
    const channelOptions=channels.map(item=>`<option value="${f(item.type)}" data-setup="${f(item.setup_mode||'')}">${f(item.name)} · ${f(item.setup_mode==='managed'?'Xvond managed':'Direct setup')}</option>`).join('');
    openModal('Add Customer Channel',`
      <div class="modal-intro"><strong>Connect this employee to a customer channel</strong><p>Choose the employee and channel. Xvond keeps the same identity, knowledge and allowed actions across every channel.</p></div>
      <div class="form-grid two">
        <div class="form-group"><label>AI Employee</label><select id="workspace-channel-agent">${agentOptions}</select></div>
        <div class="form-group"><label>Channel</label><select id="workspace-channel-type">${channelOptions}</select></div>
      </div>
      <div id="workspace-channel-note" class="source-of-truth-box"><strong>Setup</strong><div>Managed channels are provisioned by Xvond and stay inactive until the provider route is verified.</div></div>
      <button class="modal-submit" onclick="createWorkspaceChannel()">Create Channel</button>`);
    const refresh=()=>{
      const agentId=Number(document.getElementById('workspace-channel-agent')?.value||0);
      const select=document.getElementById('workspace-channel-type');
      if(!select)return;
      [...select.options].forEach(option=>{
        option.disabled=existing.has(`${agentId}:${option.value}`);
      });
      if(select.selectedOptions[0]?.disabled){
        const first=[...select.options].find(option=>!option.disabled);
        if(first)select.value=first.value;
      }
    };
    document.getElementById('workspace-channel-agent')?.addEventListener('change',refresh);
    refresh();
  }catch(error){alert(error.message)}
};

window.createWorkspaceChannel=async function(){
  if(xvondSupportMode()){alert('Support access is read-only.');return}
  const companyId=Number(xvondWorkspace.companyId);
  const agentId=Number(document.getElementById('workspace-channel-agent')?.value||0);
  const channelType=String(document.getElementById('workspace-channel-type')?.value||'').trim();
  if(!agentId||!channelType){alert('Employee and channel are required.');return}
  try{
    let config={};
    if(channelType==='voice'){
      config={provider:'vapi',language:'auto',tone:'professional_friendly',response_length:'concise',allow_interruption:true};
    }else if(!['website','whatsapp'].includes(channelType)){
      config={provisioning_state:'requested',request_source:'admin_managed_delivery'};
    }
    const created=await api(`/admin/channels/agents/${agentId}`,{
      method:'POST',
      body:JSON.stringify({channel_type:channelType,config})
    });
    closeModal();
    await loadCompanyControlCenter(companyId,'channels');
    if(channelType==='website'&&typeof openWebsiteChannel==='function'){
      openWebsiteChannel(companyId,agentId);
    }else if(channelType==='whatsapp'&&typeof openWhatsAppSetup==='function'){
      openWhatsAppSetup(agentId,created.id);
    }else if(channelType==='voice'&&typeof openVoiceSettings==='function'){
      openVoiceSettings(agentId,created.id);
    }else if(typeof openManagedChannelSetup==='function'){
      openManagedChannelSetup(created.id);
    }
  }catch(error){alert(error.message)}
};

function renderChannelsTab(){
  const d=xvondWorkspace.data;
  return `<div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Channels</h3><p>Configuration, local activation and provider connection are reported separately.</p></div><button class="primary-button" onclick="openAddWorkspaceChannel()">+ Channel</button></div>${d.agentMeta.length?d.agentMeta.map(row=>{
    const a=row.agent,web=wsChannel(a.id,'website'),wa=wsChannel(a.id,'whatsapp');
    const webState=wsChannelPresentation(web),waState=wsChannelPresentation(wa);
    return `<div class="channel-employee"><div class="channel-employee-head"><strong>${f(a.name)}</strong><span class="meta">Shared brain, knowledge and actions</span></div><div class="channel-grid"><div class="channel-card"><div><span class="channel-name">Website Chat</span>${wsPill(webState.label,webState.kind)}</div><p>${f(wsChannelDetail(web))}</p><button class="table-button" onclick="openWebsiteChannel(${d.view.company.id},${a.id})">${web?'Website Settings':'Connect Website'}</button></div><div class="channel-card"><div><span class="channel-name">WhatsApp</span>${wsPill(waState.label,waState.kind)}</div><p>${f(wsChannelDetail(wa))}</p><div class="meta">Credentials: ${wa?.configured?'Configured':'Incomplete'} · Local state: ${wa?.enabled?'Active':'Inactive'}</div><div class="agent-actions">${wa?`<button class="table-button" onclick="openWhatsAppSetup(${a.id},${wa.id})">Settings</button>${wa.connected===true?'':`<button class="primary-button" onclick="openMetaWhatsAppConnect(${a.id})">Connect with Meta</button>`}${wa.configured?`<button class="table-button" onclick="setWorkspaceChannelStatus(${wa.id},${!wa.enabled})">${wa.enabled?'Deactivate':'Activate'}</button>`:''}`:`<button class="table-button" onclick="createWhatsAppChannelForEmployee(${d.view.company.id},${a.id})">Connect WhatsApp</button>`}</div></div></div>${renderManagedChannelRequests(a.id)}</div>`;
  }).join(''):wsEmpty('Create an AI employee first')}</div>`;
}

function renderOperationsTab(){const d=xvondWorkspace.data,items=d.requests;return `<div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Business Operations</h3><p>Actual results created by configured AI actions.</p></div><div class="workspace-inline-actions">${d.agentMeta.map(x=>`<button class="table-button" onclick="openAgentActions(${d.view.company.id},${x.agent.id})">Configure ${f(x.agent.name)}</button>`).join('')}</div></div>${d.unresolved.length?`<div class="workspace-empty"><strong>${d.unresolved.length} external operation${d.unresolved.length===1?'':'s'} need reconciliation</strong><p>Verify the external CRM/POS/API before choosing an outcome. Xvond will not retry automatically.</p></div>`:''}<div class="workspace-metrics compact"><div class="metric-card"><span>Total</span><strong>${items.length}</strong></div><div class="metric-card"><span>Open</span><strong>${items.filter(x=>!['completed','cancelled'].includes(x.status)).length}</strong></div><div class="metric-card"><span>Completed</span><strong>${items.filter(x=>x.status==='completed').length}</strong></div><div class="metric-card"><span>External unresolved</span><strong>${d.unresolved.length}</strong></div></div>${items.length?`<div class="operation-list">${items.map(x=>{const unresolved=['executing','external_failed','cancelling'].includes(x.status);return `<div class="request-card"><div class="request-card-head"><div><strong>${f(x.action_type.replaceAll('_',' '))} #${x.id}</strong><div class="meta">${f(wsAgentName(x.agent_id))} · ${wsDate(x.created_at)}</div></div>${wsPill(x.status,x.status==='completed'?'good':unresolved?'bad':x.status==='cancelled'?'bad':'neutral')}</div><div class="request-summary">${f(x.summary||'')}</div><div class="request-details">${operationDetailsHtml(x.details)}</div><div class="workspace-inline-actions">${unresolved?`<button class="table-button" onclick="reconcileWorkspaceOperation(${x.id},'executed')">Verified executed</button><button class="table-button" onclick="reconcileWorkspaceOperation(${x.id},'not_executed')">Verified not executed</button><button class="table-button" onclick="reconcileWorkspaceOperation(${x.id},'cancelled')">Verified cancelled</button>`:`${!['in_progress','processing','completed','cancelled'].includes(x.status)?`<button class="table-button" onclick="updateWorkspaceOperation(${x.id},'in_progress')">Start</button>`:''}${!['completed','cancelled'].includes(x.status)?`<button class="table-button" onclick="updateWorkspaceOperation(${x.id},'completed')">Complete</button><button class="table-button" onclick="updateWorkspaceOperation(${x.id},'cancelled')">Cancel</button>`:''}`}${x.conversation_id?`<button class="table-button" onclick="openHumanConversation(${d.view.company.id},${x.agent_id},${x.conversation_id})">Conversation</button>`:''}</div></div>`}).join('')}</div>`:wsEmpty('No real operations yet')}</div>`}
function renderIntegrationsTab(){const d=xvondWorkspace.data;return `<div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Integrations</h3><p>External systems that configured actions can actually call.</p></div><button class="primary-button" onclick="openWorkspaceIntegrationForm()">+ Integration</button></div>${d.integrations.length?`<div class="integration-grid">${d.integrations.map(x=>`<div class="integration-card"><div class="integration-card-head"><div><h4>${f(x.name)}</h4><div class="meta">${f(x.integration_type)}</div></div>${wsPill(x.enabled?(x.configured?'Connected':'Needs setup'):'Disabled',x.enabled&&x.configured?'good':x.enabled?'bad':'neutral')}</div><div class="workspace-inline-actions"><button class="table-button" onclick="openWorkspaceIntegrationForm(${x.id})">Edit</button><button class="table-button" onclick="toggleWorkspaceIntegration(${x.id},${!x.enabled})">${x.enabled?'Disable':'Enable'}</button><button class="danger-link" onclick="deleteWorkspaceIntegration(${x.id})">Delete</button></div></div>`).join('')}</div>`:wsEmpty('No integrations connected')}</div>`}
function renderConversationsTab(){const d=xvondWorkspace.data,sessions=new Map((d.handoffs||[]).map(x=>[+x.conversation_id,x]));return `<div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Conversations</h3><p>Central inbox across implemented channels.</p></div></div>${d.conversations.length?`<div class="table-wrap"><table><thead><tr><th>Conversation</th><th>AI Employee</th><th>Mode</th><th>Channel</th><th>Created</th><th></th></tr></thead><tbody>${d.conversations.map(x=>{const s=sessions.get(+x.id);return `<tr><td><strong>#${x.id}</strong><div class="meta">${f(x.title||'Conversation')}</div></td><td>${f(wsAgentName(x.agent_id))}</td><td>${wsPill(s?.mode==='human'?'Human':'AI',s?.mode==='human'?'bad':'good')}</td><td>${f(s?.channel||'—')}</td><td>${wsDate(x.created_at)}</td><td><button class="table-button" onclick="openHumanConversation(${d.view.company.id},${x.agent_id},${x.id})">Open</button></td></tr>`}).join('')}</tbody></table></div>`:wsEmpty('No conversations yet')}</div>`}
function renderUsageTab(){const d=xvondWorkspace.data,u=d.usage||{},s=u.summary||{},rows=u.usage||[];return `<div class="workspace-metrics"><div class="metric-card"><span>AI Requests</span><strong>${s.requests||0}</strong></div><div class="metric-card"><span>Input Tokens</span><strong>${s.input_tokens||0}</strong></div><div class="metric-card"><span>Output Tokens</span><strong>${s.output_tokens||0}</strong></div><div class="metric-card"><span>Total Tokens</span><strong>${s.total_tokens||0}</strong></div><div class="metric-card"><span>Provider Cost</span><strong>${wsMoney(s.provider_cost)}</strong></div></div><div class="workspace-panel">${rows.length?`<div class="table-wrap"><table><thead><tr><th>Time</th><th>Employee</th><th>Provider</th><th>Model</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>${rows.slice(0,200).map(x=>`<tr><td>${wsDate(x.created_at)}</td><td>${f(wsAgentName(x.agent_id))}</td><td>${f(x.provider)}</td><td>${f(x.model)}</td><td>${x.total_tokens||0}</td><td>${wsMoney(x.provider_cost)}</td></tr>`).join('')}</tbody></table></div>`:wsEmpty('No AI usage yet')}</div>`}
function renderUsersTab(){const d=xvondWorkspace.data,users=d.users||[];return `<div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Company Users</h3><p>Accounts that can sign in to the customer portal.</p></div><button class="primary-button" onclick="openWorkspaceUserForm()">+ User</button></div>${users.length?`<div class="table-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th></th></tr></thead><tbody>${users.map(x=>`<tr><td>${f(x.full_name||'—')}</td><td>${f(x.email)}</td><td>${f(x.role)}</td><td>${wsPill(x.active===false?'Inactive':'Active',x.active===false?'bad':'good')}</td><td>${x.role==='owner'?'':`<button class="table-button" onclick="toggleWorkspaceUser(${x.id},${x.active===false})">${x.active===false?'Activate':'Disable'}</button>`}</td></tr>`).join('')}</tbody></table></div>`:wsEmpty('No company users yet')}</div>`}

function renderBillingTab(){const d=xvondWorkspace.data,services=d.billingServices||[],plans=d.plans.filter(x=>x.enabled);const serviceCodes=[...new Set(plans.map(x=>x.service_code))];return `<div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Service Billing</h3><p>Each Xvond service has its own plan, period, usage and status.</p></div><button class="primary-button" onclick="openWorkspaceServiceForm()">+ Assign Service</button></div>${services.length?`<div class="integration-grid">${services.map(s=>`<div class="integration-card"><div class="integration-card-head"><div><h4>${f(s.service_name||s.service_code)}</h4><div class="meta">${f(s.plan?.name||'No plan')} · ${f(s.plan?.tier||'')} · ${f(s.plan?.currency||'')} ${f(s.plan?.monthly_price||0)}</div></div>${wsPill(s.status,s.status==='active'?'good':s.status==='expired'?'bad':'neutral')}</div><div class="meta">Period: ${wsDate(s.current_period_start)} → ${wsDate(s.current_period_end)}</div><div class="info-stack">${Object.entries(s.usage||{}).map(([metric,row])=>`<div><span>${f(metric)}</span><strong>${f(row.used)} / ${row.limit===0||row.limit==='0'?'∞':f(row.limit)}</strong></div>`).join('')}</div><div class="workspace-inline-actions"><button class="table-button" onclick="openWorkspaceServiceForm('${s.service_code}')">Change Plan</button>${s.status==='active'?`<button class="table-button" onclick="setWorkspaceServiceStatus('${s.service_code}','paused')">Pause</button>`:`<button class="table-button" onclick="setWorkspaceServiceStatus('${s.service_code}','active')">Activate</button>`}${s.status!=='cancelled'?`<button class="table-button" onclick="setWorkspaceServiceStatus('${s.service_code}','cancelled')">Cancel</button>`:''}</div></div>`).join('')}</div>`:wsEmpty('No services assigned')}${serviceCodes.length?`<div class="workspace-panel-head"><div><h3>Available Service Plans</h3><p>${plans.length} enabled plans across ${serviceCodes.length} services.</p></div></div>`:''}</div>`}
function renderLogsTab(){const logs=xvondWorkspace.data.audit||[];return `<div class="workspace-panel"><div class="workspace-panel-head"><div><h3>Audit & Runtime Events</h3><p>Recorded actions for this company.</p></div></div>${logs.length?`<div class="table-wrap"><table><thead><tr><th>Time</th><th>Action</th><th>Resource</th><th>Details</th></tr></thead><tbody>${logs.map(x=>`<tr><td>${wsDate(x.created_at)}</td><td>${f(x.action)}</td><td>${f(x.resource_type||'—')} ${x.resource_id?`#${x.resource_id}`:''}</td><td><code>${f(JSON.stringify(x.details||{})).slice(0,260)}</code></td></tr>`).join('')}</tbody></table></div>`:wsEmpty('No audit events yet')}</div>`}

async function toggleWorkspaceCompany(active){try{if(active)await api(`/admin/production/companies/${xvondWorkspace.companyId}/activate`,{method:'POST'});else await api(`/admin/production/companies/${xvondWorkspace.companyId}/deactivate`,{method:'POST'});await loadCompanyControlCenter(xvondWorkspace.companyId,'overview')}catch(e){alert(e.message)}}
async function toggleWorkspaceCapability(name,enable){try{const row=wsModuleMap().get(name);if(enable){if(row)await api(`/admin/companies/${xvondWorkspace.companyId}/modules/${name}/enable`,{method:'POST'});else await api(`/admin/companies/${xvondWorkspace.companyId}/modules/${name}`,{method:'POST'})}else{const used=wsEnabledOperationCount(name);if(used&&!confirm(`This capability is used by ${used} enabled action(s). Continue?`))return;await api(`/admin/companies/${xvondWorkspace.companyId}/modules/${name}/disable`,{method:'POST'})}await loadCompanyControlCenter(xvondWorkspace.companyId,'capabilities')}catch(e){alert(e.message)}}
async function setWorkspaceChannelStatus(channelId,enabled){try{await api(`/admin/channels/${channelId}`,{method:'PUT',body:JSON.stringify({enabled})});await loadCompanyControlCenter(xvondWorkspace.companyId,'channels')}catch(e){alert(e.message)}}
async function deleteWorkspaceEmployee(agentId,live){if(live){alert('Deactivate all live channels first.');return}if(!confirm('Permanently delete this AI employee?'))return;try{await api(`/admin/ai-employees/companies/${xvondWorkspace.companyId}/${agentId}`,{method:'DELETE'});await loadCompanyControlCenter(xvondWorkspace.companyId,'agents')}catch(e){alert(e.message)}}
async function updateWorkspaceOperation(id,status){try{await api(`/admin/agent-actions/requests/${id}`,{method:'PATCH',body:JSON.stringify({status})});await loadCompanyControlCenter(xvondWorkspace.companyId,'operations')}catch(e){alert(e.message)}}
async function reconcileWorkspaceOperation(id,outcome){const note=prompt('Optional reconciliation note:')||'';if(!confirm(`Confirm external reconciliation outcome: ${outcome}?`))return;try{await api(`/admin/operations/requests/${id}/reconcile`,{method:'PATCH',body:JSON.stringify({outcome,note})});await loadCompanyControlCenter(xvondWorkspace.companyId,'operations')}catch(e){alert(e.message)}}

function openCompanyIdentityEditor(){const p=xvondWorkspace.data.profile,c=p.catalog||{};openModal('Company Profile',`<div class="modal-intro"><strong>Single company identity</strong><p>These values are normalized by Xvond and inherited by every AI employee.</p></div><div class="form-grid two"><div class="form-group"><label>Company Name</label><input id="cp-name" value="${f(p.company_name)}"></div><div class="form-group"><label>Business Type</label><select id="cp-type">${wsSelect(c.business_types,p.business_type,'Select business type')}</select></div><div class="form-group"><label>Country</label><select id="cp-country">${wsSelect(c.countries,p.country,'Select country')}</select></div><div class="form-group"><label>Currency</label><select id="cp-currency">${wsSelect(c.currencies,p.currency,'Select currency')}</select></div><div class="form-group"><label>Timezone</label><select id="cp-timezone">${wsSelect(c.timezones,p.timezone,'Select timezone')}</select></div><div class="form-group"><label>Primary Language</label><select id="cp-language">${wsSelect(c.languages,p.primary_language,'Select language')}</select></div><div class="form-group"><label>Additional Languages</label><input id="cp-additional-languages" value="${f((p.additional_languages||[]).join(', '))}" placeholder="English, Arabic"></div><div class="form-group"><label>Phone</label><input id="cp-phone" value="${f(p.phone||'')}"></div><div class="form-group"><label>Email</label><input id="cp-email" type="email" value="${f(p.email||'')}"></div><div class="form-group"><label>Website</label><input id="cp-website" type="url" value="${f(p.website||'')}"></div></div><div class="form-group"><label>Description</label><textarea id="cp-description">${f(p.description||'')}</textarea></div><button class="modal-submit" onclick="saveCompanyIdentityEditor()">Save Company Profile</button>`)}
function mergedCompanyProfile(overrides){const p=xvondWorkspace.data.profile;return {company_name:p.company_name,business_type:p.business_type,description:p.description,country:p.country,currency:p.currency,timezone:p.timezone,primary_language:p.primary_language,additional_languages:p.additional_languages||[],phone:p.phone,email:p.email,website:p.website,working_hours:p.working_hours||{},locations:p.locations||[],services:p.services||[],service_areas:p.service_areas||[],policies:p.policies||[],business_rules:p.business_rules||[],...overrides}}
async function saveCompanyIdentityEditor(){const v=id=>document.getElementById(id).value.trim();try{const payload=({company_name:xvondWorkspace.data.profile.company_name,company_name:v('cp-name'),business_type:v('cp-type')||null,description:v('cp-description')||null,country:v('cp-country')||null,currency:v('cp-currency')||null,timezone:v('cp-timezone')||null,primary_language:v('cp-language')||null,additional_languages:v('cp-additional-languages').split(',').map(x=>x.trim()).filter(Boolean),phone:v('cp-phone')||null,email:v('cp-email')||null,website:v('cp-website')||null});await api(`/admin/company-profile/${xvondWorkspace.companyId}`,{method:'PUT',body:JSON.stringify(payload)});closeModal();await loadCompanyControlCenter(xvondWorkspace.companyId,'company')}catch(e){alert(e.message)}}
function openBusinessInformationEditor(){const p=xvondWorkspace.data.profile,h=p.working_hours||{},days=[['mon','Monday'],['tue','Tuesday'],['wed','Wednesday'],['thu','Thursday'],['fri','Friday'],['sat','Saturday'],['sun','Sunday']];openModal('Business Information',`<div class="modal-intro"><strong>Operational company facts</strong><p>Stored once and synchronized into Business Information knowledge.</p></div><h3>Working Hours</h3><div class="hours-editor">${days.map(([key,label])=>{const x=h[key]||{};return `<div class="hours-row"><label><input id="bh-enabled-${key}" type="checkbox" ${x.enabled!==false&&x.start&&x.end?'checked':''}> ${label}</label><input id="bh-start-${key}" type="time" value="${f(x.start||'')}"><span>to</span><input id="bh-end-${key}" type="time" value="${f(x.end||'')}"></div>`}).join('')}</div><div class="form-grid two"><div class="form-group"><label>Services — one per line</label><textarea id="bh-services">${f(wsLines(p.services))}</textarea></div><div class="form-group"><label>Locations / Branches — one per line</label><textarea id="bh-locations">${f(wsLines(p.locations))}</textarea></div><div class="form-group"><label>Service Areas — one per line</label><textarea id="bh-areas">${f(wsLines(p.service_areas))}</textarea></div><div class="form-group"><label>Policies — one per line</label><textarea id="bh-policies">${f(wsLines(p.policies))}</textarea></div></div><div class="form-group"><label>Business Rules — one per line</label><textarea id="bh-rules">${f(wsLines(p.business_rules))}</textarea></div><button class="modal-submit" onclick="saveBusinessInformationEditor()">Save Business Information</button>`)}
async function saveBusinessInformationEditor(){const days=['mon','tue','wed','thu','fri','sat','sun'],hours={};days.forEach(key=>{const enabled=document.getElementById(`bh-enabled-${key}`).checked,start=document.getElementById(`bh-start-${key}`).value,end=document.getElementById(`bh-end-${key}`).value;hours[key]=enabled&&start&&end?{enabled:true,start,end}:{enabled:false}});try{const payload=({company_name:xvondWorkspace.data.profile.company_name,working_hours:hours,services:wsParseLines(document.getElementById('bh-services').value),locations:wsParseLines(document.getElementById('bh-locations').value),service_areas:wsParseLines(document.getElementById('bh-areas').value),policies:wsParseLines(document.getElementById('bh-policies').value),business_rules:wsParseLines(document.getElementById('bh-rules').value)});await api(`/admin/company-profile/${xvondWorkspace.companyId}`,{method:'PUT',body:JSON.stringify(payload)});closeModal();await loadCompanyControlCenter(xvondWorkspace.companyId,'company')}catch(e){alert(e.message)}}

function executableIntegrationDefinitions(){return (xvondWorkspace.data.setup?.integrations||[]).filter(x=>XVOND_EXECUTABLE_INTEGRATIONS.has(x.type))}
function integrationDefinition(type){return executableIntegrationDefinitions().find(x=>x.type===type)}
function openWorkspaceIntegrationForm(id=null){const current=id?xvondWorkspace.data.integrations.find(x=>+x.id===+id):null;xvondIntegrationDraft={id,current,type:current?.integration_type||executableIntegrationDefinitions()[0]?.type||''};const options=executableIntegrationDefinitions().map(x=>wsOption(x.type,xvondIntegrationDraft.type,x.name)).join('');openModal(current?'Edit Integration':'Add Integration',`<div class="modal-intro"><strong>Real external connection</strong><p>Only integration types with a runtime execution adapter are available.</p></div><div class="form-group"><label>Integration Type</label><select id="wi-type" ${current?'disabled':''} onchange="workspaceIntegrationTypeChanged()">${options}</select></div><div class="form-group"><label>Name</label><input id="wi-name" value="${f(current?.name||'')}"></div><div id="wi-fields"></div><button class="modal-submit" onclick="saveWorkspaceIntegration()">${current?'Save Integration':'Connect Integration'}</button>`);renderWorkspaceIntegrationFields()}
function workspaceIntegrationTypeChanged(){xvondIntegrationDraft.type=document.getElementById('wi-type').value;renderWorkspaceIntegrationFields()}
function renderWorkspaceIntegrationFields(){const def=integrationDefinition(xvondIntegrationDraft.type),box=document.getElementById('wi-fields');if(!def||!box)return;const current=xvondIntegrationDraft.current,config=current?.config||{},secretSet=new Set(current?.configured_secret_fields||[]);box.innerHTML=(def.config_fields||[]).map(field=>`<div class="form-group"><label>${f(field.label)}${field.required?' *':''}</label><input id="wi-field-${f(field.name)}" type="${field.secret?'password':'text'}" value="${field.secret?'':f(config[field.name]||field.default||'')}" placeholder="${field.secret&&secretSet.has(field.name)?'Configured — leave blank to keep':''}"></div>`).join('')}
async function saveWorkspaceIntegration(){const type=xvondIntegrationDraft.type,def=integrationDefinition(type),name=document.getElementById('wi-name').value.trim(),config={};for(const field of (def?.config_fields||[])){const value=document.getElementById(`wi-field-${field.name}`).value.trim();if(value||!field.secret)config[field.name]=value}try{if(xvondIntegrationDraft.id)await api(`/admin/integrations/${xvondIntegrationDraft.id}`,{method:'PATCH',body:JSON.stringify({name,config})});else await api(`/admin/integrations/companies/${xvondWorkspace.companyId}`,{method:'POST',body:JSON.stringify({integration_type:type,name,config})});closeModal();await loadCompanyControlCenter(xvondWorkspace.companyId,'integrations')}catch(e){alert(e.message)}}
async function toggleWorkspaceIntegration(id,enabled){try{await api(`/admin/integrations/${id}`,{method:'PATCH',body:JSON.stringify({enabled})});await loadCompanyControlCenter(xvondWorkspace.companyId,'integrations')}catch(e){alert(e.message)}}
async function deleteWorkspaceIntegration(id){if(!confirm('Delete this integration? Actions using it will stop working.'))return;try{await api(`/admin/integrations/${id}`,{method:'DELETE'});await loadCompanyControlCenter(xvondWorkspace.companyId,'integrations')}catch(e){alert(e.message)}}
function openWorkspaceUserForm(){openModal('Add Company User',`<div class="form-grid two"><div class="form-group"><label>Full Name</label><input id="wu-name"></div><div class="form-group"><label>Email</label><input id="wu-email" type="email"></div><div class="form-group"><label>Role</label><select id="wu-role"><option value="employee">Employee</option><option value="manager">Manager</option><option value="admin">Admin</option><option value="owner">Owner</option></select></div><div class="form-group"><label>Temporary Password</label><input id="wu-password" type="password"></div></div><button class="modal-submit" onclick="saveWorkspaceUser()">Create User</button>`)}
async function saveWorkspaceUser(){try{await api(`/admin/company-users/companies/${xvondWorkspace.companyId}`,{method:'POST',body:JSON.stringify({full_name:document.getElementById('wu-name').value.trim(),email:document.getElementById('wu-email').value.trim(),role:document.getElementById('wu-role').value,password:document.getElementById('wu-password').value})});closeModal();await loadCompanyControlCenter(xvondWorkspace.companyId,'users')}catch(e){alert(e.message)}}
async function toggleWorkspaceUser(id,active){try{await api(`/admin/company-users/${id}/status`,{method:'PATCH',body:JSON.stringify({active})});await loadCompanyControlCenter(xvondWorkspace.companyId,'users')}catch(e){alert(e.message)}}

function openWorkspaceServiceForm(serviceCode=null){const d=xvondWorkspace.data,plans=d.plans.filter(x=>x.enabled),codes=[...new Set(plans.map(x=>x.service_code))],selected=serviceCode||codes.find(code=>!d.billingServices.some(s=>s.service_code===code))||codes[0]||'';openModal('Assign Service Plan',`<div class="form-group"><label>Service</label><select id="wb-service" ${serviceCode?'disabled':''} onchange="renderWorkspaceServicePlans()">${codes.map(code=>wsOption(code,selected,plans.find(x=>x.service_code===code)?.service_code||code)).join('')}</select></div><div class="form-group"><label>Plan</label><select id="wb-plan"></select></div><button class="modal-submit" onclick="saveWorkspaceServicePlan()">Save Service Plan</button>`);renderWorkspaceServicePlans()}
function renderWorkspaceServicePlans(){const code=document.getElementById('wb-service').value,current=xvondWorkspace.data.billingServices.find(x=>x.service_code===code),plans=xvondWorkspace.data.plans.filter(x=>x.enabled&&x.service_code===code);document.getElementById('wb-plan').innerHTML=plans.map(x=>wsOption(x.id,current?.plan?.id,`${x.name} · ${x.currency} ${x.monthly_price}`)).join('')}
async function saveWorkspaceServicePlan(){const service=document.getElementById('wb-service').value,plan_id=Number(document.getElementById('wb-plan').value);try{await api(`/admin/service-billing/companies/${xvondWorkspace.companyId}/services/${service}`,{method:'PUT',body:JSON.stringify({plan_id})});closeModal();await loadCompanyControlCenter(xvondWorkspace.companyId,'billing')}catch(e){alert(e.message)}}
async function setWorkspaceServiceStatus(service,status){if(status==='cancelled'&&!confirm(`Cancel ${service} service?`))return;try{await api(`/admin/service-billing/companies/${xvondWorkspace.companyId}/services/${service}/status`,{method:'PATCH',body:JSON.stringify({status})});await loadCompanyControlCenter(xvondWorkspace.companyId,'billing')}catch(e){alert(e.message)}}

openSimpleCompany=loadCompanyControlCenter;
window.openCompany=loadCompanyControlCenter;
