(function installOperatorControlCenterPolish(){
  function finite(value){const number=Number(value||0);return Number.isFinite(number)?number:0}
  function channelState(channel){
    if(typeof wsChannelPresentation==='function')return wsChannelPresentation(channel);
    if(!channel)return {label:'Not connected',kind:'bad'};
    if(channel.enabled&&channel.connected===true)return {label:'Active',kind:'good'};
    if(channel.configured)return {label:'Configured',kind:'neutral'};
    return {label:'Needs setup',kind:'bad'};
  }

  function operatorAttentionItems(){
    const data=xvondWorkspace?.data||{};
    const items=[];

    for(const service of (data.billingServices||[]).filter(item=>item.status==='active')){
      for(const [metric,row] of Object.entries(service.usage||{})){
        const limit=finite(row?.limit),used=finite(row?.used);
        if(limit<=0)continue;
        const label=String(metric||'usage').replaceAll('_',' ');
        if(used>=limit){
          items.push({kind:'bad',title:`${service.service_name||service.service_code}: ${label} limit reached`,body:`${used} / ${limit} · period ends ${wsDate(service.current_period_end)}`,tab:'billing'});
        }else if(used/limit>=0.8){
          items.push({kind:'neutral',title:`${service.service_name||service.service_code}: ${label} at ${Math.round((used/limit)*100)}%`,body:`${used} / ${limit}`,tab:'billing'});
        }
      }
    }

    for(const channel of (data.channels||[])){
      if(channel.channel_type==='whatsapp'&&channel.enabled&&channel.connected!==true){
        items.push({kind:'bad',title:`${wsAgentName(channel.agent_id)}: WhatsApp connection needs attention`,body:channel.connection_issue||'The local channel is enabled but Meta is not connected.',tab:'channels'});
      }else if(channel.configured&&!channel.enabled){
        items.push({kind:'neutral',title:`${wsAgentName(channel.agent_id)}: ${channel.channel_type} is configured but inactive`,body:'The channel is not serving customer traffic.',tab:'channels'});
      }
    }

    if((data.unresolved||[]).length){
      items.push({kind:'bad',title:`${data.unresolved.length} external operation${data.unresolved.length===1?'':'s'} need reconciliation`,body:'Verify the external system outcome. Customer payloads remain in the tenant workspace.',tab:'operations'});
    }

    const recentFailures=(data.usage?.usage||[]).filter(item=>item.status==='failed');
    if(recentFailures.length){
      items.push({kind:'neutral',title:`${recentFailures.length} recent AI request failure${recentFailures.length===1?'':'s'}`,body:'Review provider/runtime health and the sanitized audit trail.',tab:'usage'});
    }

    return items.slice(0,8);
  }

  function attentionHtml(){
    const items=operatorAttentionItems();
    return `<div class="workspace-panel" id="xvond-operator-attention"><div class="workspace-panel-head"><div><h3>Needs Attention</h3><p>Platform, delivery and integration issues that require Xvond action. Customer content is intentionally excluded.</p></div>${wsPill(items.length?`${items.length} open`:'All clear',items.length?'neutral':'good')}</div>${items.length?`<div class="operation-list">${items.map(item=>`<div class="request-card"><div class="request-card-head"><div><strong>${f(item.title)}</strong><div class="meta">${f(item.body)}</div></div>${wsPill(item.kind==='bad'?'Action needed':'Review',item.kind)}</div><button class="table-button" onclick="switchWorkspaceTab('${item.tab}')">Open</button></div>`).join('')}</div>`:wsEmpty('Nothing needs attention','Service limits, channel delivery and external execution state look healthy.')}</div>`;
  }

  function renameOperatorTabs(){
    const labels={Operations:'Reconciliation',Logs:'Audit Trail'};
    document.querySelectorAll('.workspace-tab').forEach(button=>{
      const current=(button.textContent||'').trim();
      if(labels[current])button.textContent=labels[current];
      if(['Conversations','Customers','Notifications','Business Analytics'].includes(current))button.remove();
    });
  }

  function patchOverview(){
    if(xvondWorkspace?.tab!=='overview')return;
    const content=document.getElementById('workspace-content');
    if(!content)return;
    if(!document.getElementById('xvond-operator-attention'))content.insertAdjacentHTML('afterbegin',attentionHtml());

    const loadIssues=xvondWorkspace?.data?.loadIssues||[];
    if(loadIssues.length&&!document.getElementById('xvond-admin-load-issues')){
      content.insertAdjacentHTML('afterbegin',`<div class="workspace-panel" id="xvond-admin-load-issues"><div class="workspace-panel-head"><div><h3>Admin data partially unavailable</h3><p>The company workspace remains usable. Retry the affected section instead of blocking the entire admin.</p></div>${wsPill(`${loadIssues.length} affected`,'bad')}</div><div class="info-stack">${loadIssues.slice(0,8).map(item=>`<div><span>${f(item.label)}</span><strong>${f(item.message)}</strong></div>`).join('')}</div></div>`);
    }

    const selfService=String(xvondWorkspace?.data?.view?.company?.onboarding_source||'managed')==='self_service';
    if(selfService){
      document.querySelectorAll('#workspace-content .metric-card').forEach(card=>{
        const label=(card.querySelector('span')?.textContent||'').trim();
        if(['Capabilities','Open Operations','Conversations'].includes(label))card.remove();
      });
      const employees=xvondWorkspace?.data?.view?.agents||[];
      const readinessPanel=[...content.querySelectorAll('.workspace-panel')].find(panel=>(panel.querySelector('h3')?.textContent||'').trim()==='Production Readiness');
      if(readinessPanel){
        const ready=employees.filter(item=>item.self_service_readiness?.ready===true).length;
        const rows=employees.map(item=>{const state=item.self_service_readiness||{};const ok=state.ready===true;const blocker=(state.blockers||[])[0]||'';return `<div class="readiness-row"><span class="readiness-dot ${ok?'ok':'missing'}"></span><span style="flex:1"><strong>${f(item.name)}</strong><small style="display:block;margin-top:4px">${item.compiled?'Build compiled':'Build pending'} · ${f(state.mode||'workspace')}${blocker?` · ${f(blocker)}`:''}</small></span><strong>${ok?'Ready':'Needs setup'}</strong></div>`}).join('');
        readinessPanel.innerHTML=`<div class="workspace-panel-head"><div><h3>Self-Service Readiness</h3><p>Canonical customer launch policy: subscription, build provisioning, required connections and execution blockers.</p></div>${wsPill(`${ready}/${employees.length} ready`,ready===employees.length&&employees.length?'good':'neutral')}</div>${employees.length?`<div class="readiness-list">${rows}</div>`:wsEmpty('No self-service employee yet')}`;
      }
      const flow=content.querySelector('.architecture-flow');
      if(flow)flow.innerHTML='<span>Job Brief</span><b>→</b><span>Build Plan</span><b>→</b><span>AI Employee</span><b>→</b><span>Tools + Automations</span><b>→</b><span>Connections</span><b>→</b><span>Work</span>';
      const snapshot=[...content.querySelectorAll('h3')].find(item=>item.textContent.trim()==='Company Snapshot');
      if(snapshot){snapshot.textContent='Workspace Snapshot';const p=snapshot.parentElement?.querySelector('p');if(p)p.textContent='Operational state for this self-service tenant.'}
      return;
    }

    document.querySelectorAll('#workspace-content .metric-card').forEach(card=>{
      const label=(card.querySelector('span')?.textContent||'').trim();
      if(['Open Operations','Conversations'].includes(label))card.remove();
    });
  }

  function patchEmployeeCards(){
    if(xvondWorkspace?.tab!=='agents')return;
    const rows=xvondWorkspace?.data?.agentMeta||[];
    const channels=xvondWorkspace?.data?.channels||[];
    const selfService=String(xvondWorkspace?.data?.view?.company?.onboarding_source||'managed')==='self_service';
    if(selfService){
      const create=[...document.querySelectorAll('#workspace-content .workspace-panel-head button')].find(button=>(button.textContent||'').includes('+ AI Employee'));
      if(create)create.remove();
      const subtitle=document.querySelector('#workspace-content .workspace-panel-head p');
      if(subtitle)subtitle.textContent='Customer-built employees created from a Job Brief. Admin controls focus on readiness, runtime health and support.';
    }
    const cards=[...document.querySelectorAll('#workspace-content .employee-card')];
    cards.forEach((card,index)=>{
      const agent=rows[index]?.agent;if(!agent)return;
      const agentChannels=channels.filter(item=>+item.agent_id===+agent.id);
      const active=agentChannels.filter(item=>item.enabled===true&&item.connected===true).length;
      const stat=[...card.querySelectorAll('.employee-stats > div')].find(item=>/Channels/i.test(item.textContent||''));
      if(stat){
        const strong=stat.querySelector('strong'),label=stat.querySelector('span');
        if(strong)strong.textContent=String(active);
        if(label)label.textContent=selfService?'Live Connections':'Live Channels';
      }
      const flow=card.querySelector('.employee-flow-label');
      if(flow&&!card.querySelector('.operator-channel-summary')){
        const labels=['website','whatsapp','voice'].map(type=>{
          const channel=agentChannels.find(item=>item.channel_type===type);
          const state=channelState(channel);
          return `${type[0].toUpperCase()+type.slice(1)}: ${state.label}`;
        });
        flow.insertAdjacentHTML('afterend',`<div class="meta operator-channel-summary" style="margin-top:8px">${labels.map(f).join(' · ')}</div>`);
      }
      if(!card.querySelector('.operator-delivery-source')){
        const state=agent.self_service_readiness||{};
        const source=agent.creation_source==='self_service'?'Self-built':'Managed by Xvond';
        const extra=selfService?` · ${agent.compiled?'Build compiled':'Build pending'} · ${state.ready?'Launch ready':'Needs setup'}`:'';
        const top=card.querySelector('.employee-card-top');
        if(top)top.insertAdjacentHTML('afterend',`<div class="meta operator-delivery-source" style="margin-top:8px">${f(source+extra)}</div>`);
      }
      if(selfService){
        const buttons=[...card.querySelectorAll('.employee-actions button')];
        buttons.forEach(button=>{
          const label=(button.textContent||'').trim();
          if(['Information','Knowledge','Actions','Conversations'].includes(label))button.remove();
          if(label==='Channels')button.textContent='Connections';
        });
        if(flow)flow.textContent='Job Brief → Build Plan → Runtime capabilities → Connections → Work';
      }
    });
  }

  function patchReconciliationHeading(){
    if(xvondWorkspace?.tab!=='operations')return;
    const heading=document.querySelector('#workspace-content h3');
    if(heading&&heading.textContent.trim()==='External Reconciliation'){
      const description=heading.parentElement?.querySelector('p');
      if(description)description.textContent='Resolve uncertain external execution outcomes using technical metadata only. Customer details stay in the Customer Portal.';
    }
  }

  function patchBillingScope(){
    if(xvondWorkspace?.tab!=='billing')return;
    const content=document.getElementById('workspace-content');
    if(!content)return;
    const headings=[...content.querySelectorAll('h3')];
    const serviceHeading=headings.find(item=>item.textContent.trim()==='Service Billing');
    if(serviceHeading){
      serviceHeading.textContent='Company Subscription';
      const description=serviceHeading.parentElement?.querySelector('p');
      if(description)description.textContent='Assign, change, pause or cancel this company’s Xvond service subscriptions.';
    }
    const packagesHeading=headings.find(item=>item.textContent.trim()==='Service Packages');
    if(packagesHeading){
      packagesHeading.textContent='Global Package Catalog';
      const description=packagesHeading.parentElement?.querySelector('p');
      if(description)description.textContent='These packages are shared across Xvond. Editing a package affects every company assigned to it.';
    }
    [...content.querySelectorAll('button')].forEach(button=>{
      if(button.textContent.trim()==='+ Create Package')button.textContent='+ Create Global Package';
      if(button.textContent.trim()==='Edit Package')button.textContent='Edit Global Package';
    });
  }

  function injectGlobalPackageWarning(){
    const body=document.getElementById('modal-body');
    if(!body||body.querySelector('.global-package-warning'))return;
    const warning=document.createElement('div');
    warning.className='modal-intro global-package-warning';
    warning.innerHTML='<strong>Global Xvond package</strong><p>This plan is shared across companies. Price or limit changes affect every company currently assigned to this package.</p>';
    body.prepend(warning);
  }

  if(typeof window.openWorkspaceCreateServicePlan==='function'){
    const baseCreatePlan=window.openWorkspaceCreateServicePlan;
    window.openWorkspaceCreateServicePlan=function(){const result=baseCreatePlan.apply(this,arguments);injectGlobalPackageWarning();const title=document.getElementById('modal-title');if(title)title.textContent='Create Global Service Package';const submit=document.querySelector('#modal-body .modal-submit');if(submit)submit.textContent='Create Global Package';return result;};
  }
  if(typeof window.openWorkspaceEditServicePlan==='function'){
    const baseEditPlan=window.openWorkspaceEditServicePlan;
    window.openWorkspaceEditServicePlan=function(){const result=baseEditPlan.apply(this,arguments);injectGlobalPackageWarning();const title=document.getElementById('modal-title');if(title)title.textContent=`Edit Global Package · ${title.textContent.replace(/^Edit\s+/,'')}`;const submit=document.querySelector('#modal-body .modal-submit');if(submit)submit.textContent='Save Global Package';return result;};
  }

  const baseRender=window.renderCompanyControlCenter;
  if(typeof baseRender==='function'){
    window.renderCompanyControlCenter=function polishedOperatorControlCenter(){
      const result=baseRender.apply(this,arguments);
      renameOperatorTabs();
      patchOverview();
      patchEmployeeCards();
      patchReconciliationHeading();
      patchBillingScope();
      return result;
    };
  }
})();
