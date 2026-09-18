function xvondCustomerChannelLabel(type){
    const value=String(type||'').toLowerCase();
    const labels={
        whatsapp:'WhatsApp',
        website:'Website Chat',
        voice:'Voice / Phone',
        telegram:'Telegram',
        instagram:'Instagram DM',
        messenger:'Facebook Messenger',
        email:'Email',
        sms:'SMS',
        slack:'Slack',
        teams:'Microsoft Teams',
        custom:'Custom / API Channel'
    };
    return labels[value]||(
        value?value.replaceAll('_',' ').replace(/\b\w/g,letter=>letter.toUpperCase()):'Channel'
    );
}

function xvondCustomerChannelDelivery(channel){
    if(channel?.enabled){
        return {label:'Live',dot:'#16a34a'};
    }
    const status=String(channel?.delivery_status||'');
    if(status==='ready_for_launch')return {label:'Ready for launch',dot:'#2563eb'};
    if(status==='xvond_setup')return {label:'Xvond setup',dot:'#d97706'};
    if(status==='adapter_required')return {label:'Xvond adapter required',dot:'#d97706'};
    if(status==='cancelled')return {label:'Cancelled',dot:'#94a3b8'};
    return {label:'Setup required',dot:'#94a3b8'};
}

function xvondCustomerChannelStatusMarkup(agentId){
    const channels=(portalOverview?.channels||[]).filter(item=>Number(item.agent_id)===Number(agentId));
    if(!channels.length){
        return `
            <div class="xvond-agent-channels" style="margin-top:14px;padding-top:12px;border-top:1px solid rgba(148,163,184,.25)">
                <strong>Customer Channels</strong>
                <p class="muted" style="margin:6px 0 0">No customer channel is assigned to this AI Employee yet.</p>
            </div>
        `;
    }
    return `
        <div class="xvond-agent-channels" style="margin-top:14px;padding-top:12px;border-top:1px solid rgba(148,163,184,.25)">
            <strong>Customer Channels</strong>
            <p class="muted" style="margin:6px 0 0">The same employee identity, knowledge and allowed actions are used across these channels.</p>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:9px">
                ${channels.map(channel=>{
                    const delivery=xvondCustomerChannelDelivery(channel);
                    return `<span style="display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;border:1px solid rgba(148,163,184,.28);font-size:13px;font-weight:700"><span style="width:8px;height:8px;border-radius:50%;background:${delivery.dot}"></span>${safe(channel.name||xvondCustomerChannelLabel(channel.type))} · ${safe(delivery.label)}</span>`;
                }).join('')}
            </div>
        </div>
    `;
}

function xvondDecorateCustomerAgentsWithChannels(){
    const cards=Array.from(document.querySelectorAll('#agents-list .agent'));
    (agents||[]).forEach((agent,index)=>{
        const card=cards[index];
        if(!card||card.querySelector('.xvond-agent-channels'))return;
        card.insertAdjacentHTML('beforeend',xvondCustomerChannelStatusMarkup(agent.id));
    });
}

if(typeof loadAgents==='function'){
    const xvondChannelStatusOriginalLoadAgents=loadAgents;
    loadAgents=async function(...args){
        const result=await xvondChannelStatusOriginalLoadAgents(...args);
        xvondDecorateCustomerAgentsWithChannels();
        return result;
    };
}
