function xvondOnboardingStep(label,done,tab,detail){
  return `<div class="readiness-row"><span class="readiness-dot ${done?'ok':'missing'}"></span><span><strong>${f(label)}</strong>${detail?`<small style="display:block;margin-top:4px">${f(detail)}</small>`:''}</span><button class="table-button" onclick="switchWorkspaceTab('${tab}')">${done?'Review':'Open'}</button></div>`;
}

function xvondOnboardingState(){
  const d=xvondWorkspace.data||{};
  const profile=d.profile||{};
  const agents=d.view?.agents||[];
  const knowledgeReady=(d.agentMeta||[]).some(row=>(row.knowledge||[]).some(item=>item.enabled));
  const connectedChannels=(d.channels||[]).filter(channel=>channel.connected===true&&channel.enabled===true);
  const activeServices=(d.billingServices||[]).filter(service=>service.status==='active');
  const profileReady=Boolean(profile.business_type&&profile.country&&profile.primary_language);
  const servicesReady=activeServices.length>0;
  const agentsReady=agents.length>0;
  const readinessReady=Boolean(d.readiness?.ready);
  return {profileReady,servicesReady,agentsReady,knowledgeReady,channelsReady:connectedChannels.length>0,readinessReady};
}

function xvondInjectOnboardingWorkflow(){
  if(typeof xvondWorkspace==='undefined'||xvondWorkspace.tab!=='overview'||!xvondWorkspace.data)return;
  if(String(xvondWorkspace.data?.view?.company?.onboarding_source||'managed')==='self_service')return;
  const content=document.getElementById('workspace-content');
  if(!content||content.querySelector('[data-xvond-onboarding-workflow]'))return;
  const state=xvondOnboardingState();
  const company=xvondWorkspace.data.view?.company||{};
  const lifecycle=String(company.lifecycle_status||'onboarding').toLowerCase();
  const complete=[state.profileReady,state.servicesReady,state.agentsReady,state.knowledgeReady,state.channelsReady,state.readinessReady].filter(Boolean).length;
  const panel=document.createElement('div');
  panel.className='workspace-panel';
  panel.dataset.xvondOnboardingWorkflow='1';
  panel.innerHTML=`
    <div class="workspace-panel-head">
      <div><h3>Customer Onboarding</h3><p>${complete}/6 operational gates complete · Lifecycle: ${f(lifecycle)}</p></div>
      ${lifecycle==='onboarding'?'<button class="primary-button" onclick="setWorkspaceLifecycle(\'testing\')">Move to Testing</button>':lifecycle==='testing'&&state.readinessReady?'<button class="primary-button" onclick="setWorkspaceLifecycle(\'live\')">Go Live</button>':''}
    </div>
    <div class="readiness-list">
      ${xvondOnboardingStep('Service package assigned',state.servicesReady,'billing','Canonical ServiceSubscription controls commercial entitlement and limits.')}
      ${xvondOnboardingStep('Business Profile complete',state.profileReady,'company','Company facts are shared across every AI employee and channel.')}
      ${xvondOnboardingStep('AI Employee created',state.agentsReady,'agents','Create from a reusable Xvond template or configure a custom employee.')}
      ${xvondOnboardingStep('Knowledge ready',state.knowledgeReady,'knowledge','At least one enabled knowledge source is available to the employee.')}
      ${xvondOnboardingStep('Production channel connected',state.channelsReady,'channels','At least one verified and active customer-facing channel is connected.')}
      ${xvondOnboardingStep('Production Readiness passed',state.readinessReady,'overview','Xvond readiness gates must pass before Live.')}
    </div>
    ${!state.agentsReady?'<div class="workspace-inline-actions" style="margin-top:14px"><button class="primary-button" onclick="openWorkspaceTemplateAgentForm()">Create AI Employee from Template</button><button class="table-button" onclick="openWorkspaceTemplateManager()">Manage Templates</button></div>':''}`;
  content.prepend(panel);
}

async function openWorkspaceTemplateAgentForm(){
  try{
    const result=await api('/admin/agent-factory/templates');
    const templates=(result.templates||[]).filter(item=>item.enabled!==false);
    if(!templates.length){openWorkspaceTemplateManager();return;}
    openModal('Create AI Employee from Template',`
      <div class="form-group"><label>Template</label><select id="xvond-template-id">${templates.map(item=>`<option value="${Number(item.id)}">${f(item.name)} · ${f(item.category)}</option>`).join('')}</select></div>
      <div class="form-group"><label>Employee Name</label><input id="xvond-template-agent-name" placeholder="Optional — uses template name"></div>
      <p class="meta">The template supplies the base role, provider/model defaults and standard tool/channel assignments. Company facts still come from Business Profile and Knowledge.</p>
      <button class="modal-submit" onclick="createWorkspaceAgentFromTemplate()">Create Employee</button>`);
  }catch(error){alert(error.message)}
}

async function createWorkspaceAgentFromTemplate(){
  const templateId=Number(document.getElementById('xvond-template-id')?.value||0);
  const name=document.getElementById('xvond-template-agent-name')?.value?.trim()||null;
  if(!templateId)return;
  try{
    await api(`/admin/agent-factory/companies/${xvondWorkspace.companyId}/from-template`,{method:'POST',body:JSON.stringify({template_id:templateId,name})});
    closeModal();
    await loadCompanyControlCenter(xvondWorkspace.companyId,'agents');
  }catch(error){alert(error.message)}
}

async function openWorkspaceTemplateManager(){
  try{
    const result=await api('/admin/agent-factory/templates');
    const templates=result.templates||[];
    openModal('AI Employee Templates',`
      <p class="meta">Templates are Xvond-owned operational blueprints. They do not contain customer business facts.</p>
      ${templates.length?`<div class="info-stack">${templates.map(item=>`<div><span>${f(item.category)}</span><strong>${f(item.name)}</strong><small>${f(item.description||'')}</small></div>`).join('')}</div>`:'<div class="workspace-empty"><strong>No templates yet</strong><p>Create the first reusable employee blueprint below.</p></div>'}
      <hr style="margin:18px 0">
      <div class="form-grid two"><div class="form-group"><label>Name</label><input id="xvond-template-name" placeholder="Customer Support"></div><div class="form-group"><label>Category</label><select id="xvond-template-category"><option value="customer_service">Customer Service</option><option value="sales">Sales</option><option value="booking">Booking</option><option value="website">Website</option><option value="whatsapp">WhatsApp</option><option value="voice">Voice</option><option value="knowledge_assistant">Knowledge Assistant</option><option value="custom">Custom</option></select></div></div>
      <div class="form-grid two"><div class="form-group"><label>Provider</label><input id="xvond-template-provider" placeholder="openai"></div><div class="form-group"><label>Model</label><input id="xvond-template-model" placeholder="Production model"></div></div>
      <div class="form-group"><label>Description</label><input id="xvond-template-description" placeholder="Reusable employee blueprint"></div>
      <div class="form-group"><label>Base System Prompt</label><textarea id="xvond-template-prompt" placeholder="Define role and operating rules only. Do not hard-code customer facts."></textarea></div>
      <button class="modal-submit" onclick="createWorkspaceTemplate()">Create Template</button>`);
  }catch(error){alert(error.message)}
}

async function createWorkspaceTemplate(){
  const payload={
    name:document.getElementById('xvond-template-name')?.value?.trim()||'',
    category:document.getElementById('xvond-template-category')?.value||'custom',
    description:document.getElementById('xvond-template-description')?.value?.trim()||null,
    default_system_prompt:document.getElementById('xvond-template-prompt')?.value?.trim()||'',
    default_provider:document.getElementById('xvond-template-provider')?.value?.trim()||'',
    default_model:document.getElementById('xvond-template-model')?.value?.trim()||'',
    default_config:{}
  };
  if(!payload.name||!payload.default_system_prompt||!payload.default_provider||!payload.default_model){alert('Name, provider, model and base system prompt are required.');return;}
  try{
    await api('/admin/agent-factory/templates',{method:'POST',body:JSON.stringify(payload)});
    await openWorkspaceTemplateAgentForm();
  }catch(error){alert(error.message)}
}

const xvondOnboardingObserver=new MutationObserver(()=>xvondInjectOnboardingWorkflow());
window.addEventListener('DOMContentLoaded',()=>{
  const target=document.getElementById('company-detail');
  if(target)xvondOnboardingObserver.observe(target,{childList:true,subtree:true});
  xvondInjectOnboardingWorkflow();
});
