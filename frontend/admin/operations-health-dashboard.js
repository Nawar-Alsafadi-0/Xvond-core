const xvondBaseLoadDashboard=loadDashboard;

function xvondHealthCard(label,value,detail=''){
  return `<div class="card"><div class="card-label">${escapeAdmin(label)}</div><div class="card-value">${escapeAdmin(value)}</div>${detail?`<small>${escapeAdmin(detail)}</small>`:''}</div>`;
}

function xvondFormatBackupRow(row){
  if(!row)return 'Unknown';
  if(row.status==='not_configured')return 'Not configured';
  if(row.status==='healthy')return 'Healthy';
  if(row.status==='stale')return 'Stale';
  return 'Missing';
}

function xvondBackupAge(row){
  const seconds=Number(row?.age_seconds);
  if(!Number.isFinite(seconds)||seconds<0)return '';
  if(seconds<3600)return `${Math.round(seconds/60)}m ago`;
  if(seconds<86400)return `${Math.round(seconds/3600)}h ago`;
  return `${Math.round(seconds/86400)}d ago`;
}

loadDashboard=async function(){
  await xvondBaseLoadDashboard();
  const target=document.getElementById('dashboard-cards');
  if(!target)return;
  try{
    const [backup,summary]=await Promise.all([
      api('/admin/operations/backups/status'),
      api('/admin/dashboard/summary')
    ]);
    const backupState=backup.status==='healthy'?'Healthy':'Attention Required';
    const localDetail=`Local: ${xvondFormatBackupRow(backup.local)}${xvondBackupAge(backup.local)?` · ${xvondBackupAge(backup.local)}`:''}`;
    const offsiteDetail=`Offsite: ${xvondFormatBackupRow(backup.offsite)}${xvondBackupAge(backup.offsite)?` · ${xvondBackupAge(backup.offsite)}`:''}`;
    target.insertAdjacentHTML('beforeend',[
      xvondHealthCard('Backup Health',backupState,`${localDetail} · ${offsiteDetail}`),
      xvondHealthCard('WhatsApp Deliveries to Review',adminNumber(summary.unresolved_whatsapp_deliveries||0),'Failed or unknown transport outcomes'),
      xvondHealthCard('Managed Channel Deliveries to Review',adminNumber(summary.unresolved_managed_channel_deliveries||0),'Telegram, Instagram, Email and other managed channel outcomes'),
    ].join(''));
  }catch(error){
    target.insertAdjacentHTML('beforeend',xvondHealthCard('Operations Health','Unavailable','Backup/delivery health check failed'));
  }
};
