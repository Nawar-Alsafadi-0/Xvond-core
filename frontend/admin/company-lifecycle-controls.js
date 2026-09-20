const XVOND_COMPANY_LIFECYCLE_OPTIONS=[
  ['onboarding','Onboarding'],
  ['testing','Testing'],
  ['live','Live'],
  ['paused','Paused'],
  ['suspended','Suspended'],
  ['cancelled','Cancelled'],
  ['archived','Archived']
];

function xvondLifecycleKind(status){
  const value=String(status||'onboarding').toLowerCase();
  if(value==='live')return 'good';
  if(['suspended','cancelled','archived'].includes(value))return 'bad';
  return 'neutral';
}

function xvondLifecycleLabel(status){
  const value=String(status||'onboarding').toLowerCase();
  return XVOND_COMPANY_LIFECYCLE_OPTIONS.find(([key])=>key===value)?.[1]||value;
}

async function setWorkspaceLifecycle(status){
  const companyId=Number(xvondWorkspace?.companyId||0);
  if(!companyId)return;
  const next=String(status||'').toLowerCase();
  const current=String(xvondWorkspace?.data?.view?.company?.lifecycle_status||'onboarding').toLowerCase();
  if(!next||next===current)return;
  const destructive=['suspended','cancelled','archived'];
  if(destructive.includes(next)&&!confirm(`Move this company to ${xvondLifecycleLabel(next)}? Customer portal access will be blocked.`))return;
  try{
    await api(`/admin/companies/${companyId}/lifecycle`,{method:'PATCH',body:JSON.stringify({status:next})});
    await loadCompanyControlCenter(companyId,'overview');
  }catch(error){
    alert(error.message);
  }
}

async function xvondEmergencyStop(){
  const companyId=Number(xvondWorkspace?.companyId||0);
  if(!companyId)return;
  if(!confirm('Emergency Stop will stop company runtime and disable every AI employee immediately. Continue?'))return;
  try{
    await api(`/admin/companies/${companyId}/status`,{method:'PATCH',body:JSON.stringify({active:false})});
    await loadCompanyControlCenter(companyId,'overview');
  }catch(error){
    alert(error.message);
  }
}

function xvondInjectLifecycleControls(){
  if(typeof xvondWorkspace==='undefined'||!xvondWorkspace?.data?.view?.company)return;
  const hero=document.querySelector('#company-detail .workspace-hero');
  if(!hero)return;
  // This function runs from a MutationObserver watching #company-detail.
  // Rewriting an existing panel would create another observed mutation and
  // keep the browser in an endless observer -> innerHTML -> observer loop.
  if(hero.querySelector('[data-xvond-lifecycle-controls]'))return;
  const company=xvondWorkspace.data.view.company;
  const selfService=String(company.onboarding_source||'managed')==='self_service';
  if(selfService){
    const panel=document.createElement('div');
    panel.dataset.xvondLifecycleControls='1';
    panel.className='workspace-panel';
    panel.style.marginTop='12px';
    panel.style.width='100%';
    hero.appendChild(panel);
    const lifecycle=String(company.lifecycle_status||'onboarding').toLowerCase();
    const runtime=company.active===true;
    panel.innerHTML=`<div class='workspace-panel-head'><div><h3>Self-Service Workspace State</h3><p>Account lifecycle and employee runtime are shown separately. Customer launch uses the self-service readiness policy, not Managed Delivery gates.</p></div>${wsPill(xvondLifecycleLabel(lifecycle),xvondLifecycleKind(lifecycle))}</div><div class='workspace-grid two-col'><div><label>Delivery model</label><div>${wsPill('Self-Service','good')}</div><p class='meta'>The customer builds and launches employees through the Xvond platform.</p></div><div><label>Runtime</label><div>${wsPill(runtime?'Running':'Stopped',runtime?'good':'bad')}</div><p class='meta'>Runtime is controlled by self-service employee readiness and launch state.</p>${runtime?'<button class="table-button" onclick="xvondEmergencyStop()">Emergency Stop</button>':''}</div></div>`;
    return;
  }
  const panel=document.createElement('div');
  panel.dataset.xvondLifecycleControls='1';
  panel.className='workspace-panel';
  panel.style.marginTop='12px';
  panel.style.width='100%';
  hero.appendChild(panel);
  const lifecycle=String(company.lifecycle_status||'onboarding').toLowerCase();
  const runtime=company.active===true;
  panel.innerHTML=`
    <div class="workspace-panel-head">
      <div>
        <h3>Company Lifecycle</h3>
        <p>Commercial/onboarding state is separate from AI runtime.</p>
      </div>
      ${wsPill(xvondLifecycleLabel(lifecycle),xvondLifecycleKind(lifecycle))}
    </div>
    <div class="workspace-grid two-col">
      <div>
        <label>Lifecycle</label>
        <select id="xvond-company-lifecycle" onchange="setWorkspaceLifecycle(this.value)">
          ${XVOND_COMPANY_LIFECYCLE_OPTIONS.map(([value,label])=>`<option value="${value}" ${value===lifecycle?'selected':''}>${label}</option>`).join('')}
        </select>
        <p class="meta">Onboarding and Testing keep customer portal access available while AI runtime stays stopped. Live is readiness-gated.</p>
      </div>
      <div>
        <label>AI Runtime</label>
        <div>${wsPill(runtime?'Running':'Stopped',runtime?'good':'bad')}</div>
        <p class="meta">Runtime follows lifecycle. Emergency Stop disables every AI employee without changing the commercial lifecycle.</p>
        ${runtime?'<button class="table-button" onclick="xvondEmergencyStop()">Emergency Stop</button>':''}
      </div>
    </div>`;

  const legacyActions=hero.querySelector('.workspace-hero-actions');
  if(legacyActions){
    [...legacyActions.querySelectorAll('button')].forEach(button=>{
      const text=String(button.textContent||'').trim().toLowerCase();
      if(text==='activate'||text==='deactivate')button.style.display='none';
    });
    [...legacyActions.querySelectorAll('.workspace-pill')].forEach(pill=>{
      const text=String(pill.textContent||'').trim().toLowerCase();
      if(text==='active'||text==='inactive')pill.style.display='none';
    });
  }
}

const xvondLifecycleObserver=new MutationObserver(()=>xvondInjectLifecycleControls());
window.addEventListener('DOMContentLoaded',()=>{
  const target=document.getElementById('company-detail');
  if(target)xvondLifecycleObserver.observe(target,{childList:true,subtree:true});
  xvondInjectLifecycleControls();
});
