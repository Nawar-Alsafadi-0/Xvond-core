function xvondEmployeeReadiness(agentId){
  return (xvondWorkspace?.data?.readiness?.agents||[]).find(item=>Number(item.id)===Number(agentId))||null;
}

function xvondEmployeeProductionLabel(agent,readiness){
  if(readiness?.ready_for_customer)return ['Customer Traffic Live','good'];
  if(agent.enabled)return ['Employee Live · Channel Pending','neutral'];
  if(readiness?.setup_ready)return ['Setup Ready','neutral'];
  return ['Draft','bad'];
}

async function xvondGoLiveEmployee(agentId){
  const company=xvondWorkspace?.data?.view?.company||{};
  const lifecycle=String(company.lifecycle_status||'onboarding').toLowerCase();
  if(lifecycle!=='live'||company.active!==true){
    alert('Move the company to Live first. Company Go Live is readiness-gated and enables the runtime; employee Go Live is a separate Delivery Readiness gate.');
    return;
  }
  try{
    await api(`/admin/delivery-readiness/companies/${xvondWorkspace.companyId}/agents/${agentId}/go-live`,{method:'POST'});
    await loadCompanyControlCenter(xvondWorkspace.companyId,'agents');
  }catch(error){alert(error.message)}
}

async function xvondPauseEmployee(agentId){
  if(!confirm('Pause this AI employee? Customer traffic for this employee will stop.'))return;
  try{
    await api(`/admin/delivery-readiness/companies/${xvondWorkspace.companyId}/agents/${agentId}/deactivate`,{method:'POST'});
    await loadCompanyControlCenter(xvondWorkspace.companyId,'agents');
  }catch(error){alert(error.message)}
}

function xvondInjectEmployeeProductionControls(){
  if(typeof xvondWorkspace==='undefined'||xvondWorkspace.tab!=='agents'||!xvondWorkspace.data)return;
  if(String(xvondWorkspace.data?.view?.company?.onboarding_source||'managed')==='self_service')return;
  const content=document.getElementById('workspace-content');
  if(!content||content.querySelector('[data-xvond-employee-production]'))return;
  const agents=xvondWorkspace.data.view?.agents||[];
  const company=xvondWorkspace.data.view?.company||{};
  const lifecycle=String(company.lifecycle_status||'onboarding').toLowerCase();
  const panel=document.createElement('div');
  panel.className='workspace-panel';
  panel.dataset.xvondEmployeeProduction='1';
  panel.style.marginBottom='18px';
  panel.innerHTML=`
    <div class="workspace-panel-head">
      <div><h3>Employee Production State</h3><p>Creating/configuring an employee never activates production traffic. Delivery Readiness owns Go Live.</p></div>
      ${wsPill(`Company: ${lifecycle}`,lifecycle==='live'?'good':'neutral')}
    </div>
    ${agents.length?`<div class="readiness-list">${agents.map(agent=>{
      const readiness=xvondEmployeeReadiness(agent.id);
      const [label,kind]=xvondEmployeeProductionLabel(agent,readiness);
      const setupReady=readiness?.setup_ready===true;
      const issues=[...(readiness?.issues||[]),...(readiness?.warnings||[])];
      let action='';
      if(agent.enabled){
        action=`<button class="table-button" onclick="xvondPauseEmployee(${Number(agent.id)})">Pause Employee</button>`;
        if(!readiness?.ready_for_customer)action+=`<button class="primary-button" onclick="switchWorkspaceTab('channels')">Open Channels</button>`;
      }else if(setupReady&&company.active===true&&lifecycle==='live'){
        action=`<button class="primary-button" onclick="xvondGoLiveEmployee(${Number(agent.id)})">Go Live</button>`;
      }else{
        action=`<button class="table-button" onclick="switchWorkspaceTab('${setupReady?'overview':'agents'}')">${setupReady?'Company must be Live':'Resolve Setup'}</button>`;
      }
      return `<div class="readiness-row"><span class="readiness-dot ${readiness?.ready_for_customer?'ok':setupReady?'ok':'missing'}"></span><span style="flex:1"><strong>${f(agent.name)}</strong><small style="display:block;margin-top:4px">${setupReady?'Setup ready':'Setup incomplete'} · ${Number(readiness?.configured_channel_count||0)} configured channel(s) · ${Number(readiness?.live_channel_count||0)} live channel(s)${issues.length?` · ${f(issues[0])}`:''}</small></span>${wsPill(label,kind)}<span class="workspace-inline-actions">${action}</span></div>`;
    }).join('')}</div>`:wsEmpty('No AI employees yet','Create an employee from a template or custom configuration first.')}`;
  content.prepend(panel);
}

const xvondEmployeeProductionObserver=new MutationObserver(()=>xvondInjectEmployeeProductionControls());
window.addEventListener('DOMContentLoaded',()=>{
  const target=document.getElementById('company-detail');
  if(target)xvondEmployeeProductionObserver.observe(target,{childList:true,subtree:true});
  xvondInjectEmployeeProductionControls();
});
