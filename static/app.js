'use strict';
const $=id=>document.getElementById(id), guestMatch=location.pathname.match(/^\/s\/([^/]+)\/([^/]+)$/);
let csrf='',status=null,folder='',root='shared',guest=null,peer=null,busy=false,startTime=0;
let remoteLink='',remoteInfo=null,serverJob=null;
const token=guestMatch?(location.hash.slice(1)||sessionStorage.getItem('fjorddrop:'+location.pathname)||''):'';
// Keep the capability out of browser history entries copied without the explicit link.
if(guestMatch&&token){sessionStorage.setItem('fjorddrop:'+location.pathname,token);history.replaceState(null,'',location.pathname);}
function notice(message){$('notice').textContent=message;$('notice').hidden=!message;}
function bytes(n){if(!n)return '0 B';const i=Math.min(4,Math.floor(Math.log(n)/Math.log(1024)));return (n/1024**i).toFixed(i?1:0)+' '+['B','KB','MB','GB','TB'][i];}
function element(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
async function api(path,body,method){const options={method:method||(body?'POST':'GET'),headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}};if(body)options.body=JSON.stringify(body);const response=await fetch(path,options);const text=await response.text();let result;try{result=JSON.parse(text);}catch{result={error:text||'Forespørgslen fejlede.'};}if(!response.ok)throw Object.assign(new Error(result.error||'Forespørgslen fejlede.'),{status:response.status});return result;}
function guestAPI(node,sid,key){return async(action,body={})=>{const response=await fetch(`/api/guest/${encodeURIComponent(node)}/${encodeURIComponent(sid)}/${action}`,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+key},body:JSON.stringify(body)});const text=await response.text();let result;try{result=JSON.parse(text);}catch{result={error:text};}if(!response.ok)throw new Error(result.error||'Delingen kunne ikke åbnes.');return result;};}
const guestCall=guestMatch?guestAPI(guestMatch[1],guestMatch[2],token):null;
function safe(fn){return async event=>{try{await fn(event);}catch(error){if(error.name!=='AbortError')notice(error.message);}};}
function displayFiles(files){
  $('files').replaceChildren();$('up').disabled=!folder;$('breadcrumb').textContent='/'+folder;
  if(!files.length){const box=element('div',undefined,'empty');box.append(element('strong','Her er plads til noget godt.'),element('span',guestMatch?'Der er ingen filer i denne mappe endnu.':root==='shared'?'Læg filer i den værtsmappe, du monterede som /shared.':'Modtagne filer vises her, når overførslen er færdig.'));$('files').append(box);return;}
  for(const file of files){const row=element('div',undefined,'file-row');const action=element('button',file.directory?'Åbn →':'Hent til pc','quiet');action.onclick=safe(async()=>{if(file.directory){folder=file.path;await loadFiles();}else await download(file);});row.append(element('span',file.directory?'▱':'▤','file-icon'),element('span',file.name,'file-name'),element('span',file.directory?'Mappe':bytes(file.size),'file-size'),action);if(remoteLink&&!file.directory){const save=element('button','Gem på server','secondary');save.onclick=safe(()=>serverDownload(file));const actions=element('div',undefined,'file-actions');action.replaceWith(actions);actions.append(action,save);}$('files').append(row);}
}
async function loadFiles(){
  $('refresh').disabled=true;
  try{
    if(guestMatch){guest=await guestCall('browse',{path:folder});$('library-title').textContent=guest.name;$('upload-area').hidden=!guest.upload;$('expiry').textContent='Udløber '+new Date(guest.expires*1000).toLocaleString('da-DK');displayFiles(guest.files);}
    else if(remoteLink){remoteInfo=await api('/api/remote/browse',{url:remoteLink,path:folder});$('library-title').textContent=remoteInfo.name+' · ekstern deling';displayFiles(remoteInfo.files);}
    else{$('library-title').textContent='Filer klar til afgang';displayFiles((await api('/api/files?root='+encodeURIComponent(root)+'&path='+encodeURIComponent(folder))).files);}
  }finally{$('refresh').disabled=false;}
}
async function loadStatus(){
  status=await api('/api/status');csrf=status.csrf;
  $('connection-state').textContent=status.settings.relay_url?'Mødested: '+status.relay_status:'Din private filstation';
  $('shares').replaceChildren();
  if(!status.shares.length){$('shares').append(element('div','Ingen delingslinks endnu. Opret et link fra den mappe, du vil dele.','empty'));return;}
  for(const share of status.shares){const row=element('div',undefined,'share-row'),copy=element('div');copy.append(element('strong',share.name),element('p',`${share.root==='shared'?'/shared':'/received'}/${share.path} · ${share.upload?'Hent & send':'Kun download'} · ${share.expires*1000<Date.now()?'Udløbet':new Date(share.expires*1000).toLocaleString('da-DK')}`));const revoke=element('button','Tilbagekald','quiet danger');revoke.onclick=safe(async()=>{if(!confirm('Tilbagekald linket? Aktive forbindelser til denne deling lukkes. Filerne bevares.'))return;await api('/api/shares/'+share.id,null,'DELETE');await loadStatus();});row.append(copy,revoke);$('shares').append(row);}
}
function progress(done,total){const percent=total?Math.min(100,done/total*100):100;$('progress').value=percent;$('percent').textContent=percent.toFixed(0)+' %';$('speed').textContent=bytes(done)+' / '+bytes(total)+' · '+bytes(done/Math.max(.1,(performance.now()-startTime)/1000))+'/s';}
async function endpoint(){
  if(guestMatch)return {call:guestCall,info:guest};
  if(remoteLink)return {call:(action,body={})=>api('/api/remote/'+action,{url:remoteLink,...body}),info:remoteInfo};
  const share=await api('/api/shares',{name:'Midlertidig pc-overførsel',root,path:'',hours:1,upload:false});
  // Admin uses its own instance even when outgoing invitations use a rendezvous.
  const link=new URL(share.url),key=link.hash.slice(1);const call=guestAPI('local',share.id,key);const info=await call('browse');return {call,info,temporary:share.id};
}
async function download(file){
  if(busy){notice('Vent til den aktuelle overførsel er færdig.');return;}
  let writer=null,temp=null;
  // File picker must be invoked during the user's click, before network awaits.
  if(window.showSaveFilePicker){const handle=await showSaveFilePicker({suggestedName:file.name});writer=await handle.createWritable();}
  else if(file.size>BLOB_LIMIT){notice('Filen er over 256 MB. Åbn linket i Chrome eller Edge via HTTPS for at gemme direkte på disk.');return;}
  begin(file.name);
  try{const ep=await endpoint();temp=ep.temporary;peer=new FilePeer(ep.call,ep.info.ice,progress);$('connection-detail').textContent=await peer.connect();startTime=performance.now();$('transfer-description').textContent='Modtager og kontrollerer hver bid …';await peer.download(file,writer);writer=null;complete('Filen er hentet og kontrolleret.');}
  catch(error){if(writer)await writer.abort().catch(()=>{});failure(error);}
  finally{peer?.close();peer=null;end();if(temp){await api('/api/shares/'+temp,null,'DELETE').catch(()=>{});await loadStatus().catch(()=>{});}}
}
function begin(name){busy=true;notice('');$('transfer-title').textContent=name;$('transfer-description').textContent='Opretter krypteret forbindelse …';$('progress').value=0;$('percent').textContent='0 %';$('speed').textContent='Forbinder';$('cancel').hidden=false;startTime=performance.now();$('upload').disabled=true;}
function complete(text){$('transfer-description').textContent=text;$('progress').value=100;$('percent').textContent='100 %';}
function failure(error){$('transfer-description').textContent=error.message;notice(error.message);}
function end(){busy=false;$('cancel').hidden=true;$('upload').disabled=false;}
$('cancel').onclick=safe(async()=>{if(serverJob)await api('/api/jobs/'+serverJob,null,'DELETE');else peer?.fail(new Error('Overførslen blev annulleret. Ingen delvis fil udgives.'));});
async function serverDownload(file){
  if(busy){notice('Vent til den aktuelle overførsel er færdig.');return;}
  begin(file.name);
  try{
    const result=await api('/api/remote/download',{url:remoteLink,path:file.path});serverJob=result.id;
    $('transfer-description').textContent='Serveren henter direkte til /received …';
    while(true){const job=await api('/api/jobs/'+serverJob);progress(job.bytes,job.size);if(job.state==='complete'){complete('Gemt på serveren: /received/'+job.path);break;}if(job.state!=='running')throw new Error(job.error||'Overførslen blev afbrudt.');await delay(700);}
  }catch(error){failure(error);}finally{serverJob=null;end();}
}
$('upload').onchange=safe(async event=>{
  if(busy)return;const files=[...event.target.files];
  for(const file of files){
    if(file.size>guest.max_upload){notice('Filen overstiger afsenderens grænse på '+bytes(guest.max_upload));break;}
    begin(file.name);
    try{peer=new FilePeer(guestCall,guest.ice,progress);$('connection-detail').textContent=await peer.connect();startTime=performance.now();$('transfer-description').textContent='Sender til afsenderens destinationsmappe …';const result=await peer.upload(file);complete('Gemt i destinationsmappen: '+result.path);}
    catch(error){failure(error);break;}
    finally{peer?.close();peer=null;end();}
  }
  event.target.value='';
});
$('refresh').onclick=safe(loadFiles);$('up').onclick=safe(async()=>{folder=folder.split('/').slice(0,-1).join('/');await loadFiles();});
$('root').onchange=safe(async event=>{remoteLink='';root=event.target.value;folder='';$('root').querySelector('option[value="remote"]')?.remove();await loadFiles();});
$('new-share').onclick=()=>{if(remoteLink){notice('Vælg først en af dine egne mapper i biblioteket.');return;}$('share-path').textContent=(root==='shared'?'/shared/':'/received/')+folder;$('share-dialog').showModal();};
$('remote-open').onclick=()=>$('remote-dialog').showModal();
$('remote-form').onsubmit=safe(async event=>{event.preventDefault();const link=new FormData(event.target).get('url');const info=await api('/api/remote/browse',{url:link});remoteLink=link;remoteInfo=info;folder='';$('root').querySelector('option[value="remote"]')?.remove();const option=element('option','Ekstern deling');option.value='remote';$('root').append(option);$('root').value='remote';$('remote-dialog').close();await loadFiles();});
$('share-form').onsubmit=safe(async event=>{event.preventDefault();const f=new FormData(event.target);const result=await api('/api/shares',{name:f.get('name'),hours:Number(f.get('hours')),upload:f.get('upload')==='on',root,path:folder});$('share-dialog').close();$('share-link').value=result.url;$('link-warning').textContent=result.url.startsWith('https://')?'Linket indeholder en hemmelig nøgle. Alle med linket får den valgte adgang.':'Dette er et lokalt HTTP-link. For deling over internettet: konfigurér HTTPS eller et offentligt mødested under Forbindelse & mapper.';$('link-dialog').showModal();await loadStatus();});
$('copy-link').onclick=safe(async()=>{if(navigator.clipboard){await navigator.clipboard.writeText($('share-link').value);$('copy-link').textContent='Kopieret ✓';}else{$('share-link').select();document.execCommand('copy');$('copy-link').textContent='Link markeret / kopieret';}});
$('login-form').onsubmit=safe(async event=>{event.preventDefault();const f=new FormData(event.target);const result=await api('/api/login',{username:f.get('username'),password:f.get('password')});csrf=result.csrf;event.target.reset();await init();});
$('logout').onclick=safe(async()=>{await api('/api/logout',{});location.reload();});
$('settings-open').onclick=()=>{for(const [key,value]of Object.entries(status.settings)){const field=$('settings-form').elements.namedItem(key);if(field)field.value=value;}$('settings-form').elements.turn_password.value='';$('settings-form').elements.relay_key.value='';$('settings-dialog').showModal();};
$('settings-form').onsubmit=safe(async event=>{event.preventDefault();const values=Object.fromEntries(new FormData(event.target));if(!values.turn_password)delete values.turn_password;if(!values.relay_key)delete values.relay_key;await api('/api/settings',values);$('settings-dialog').close();await loadStatus();notice('Indstillinger gemt. En udgående parring forbinder automatisk inden for få sekunder.');});
$('unpair').onclick=safe(async()=>{if(!confirm('Fjern den udgående parring? Eksisterende links gennem mødestedet vil ikke virke.'))return;await api('/api/settings',{relay_url:'',relay_id:'',relay_key:''});for(const k of ['relay_url','relay_id','relay_key'])$('settings-form').elements[k].value='';await loadStatus();});
$('pair-key').onclick=safe(async()=>{const result=await api('/api/relay-key',{});$('pair-result-label').hidden=false;$('pair-result').value=`HTTPS-adresse: ${result.url||'Angiv offentlig adresse og gem først'}\nParrings-ID: ${result.id}\nParringsnøgle: ${result.key}`;});
$('revoke-pair').onclick=safe(async()=>{const id=$('revoke-pair-id').value.trim();if(!id||!confirm('Tilbagekald denne parring? Den lokale app mister forbindelsen.'))return;await api('/api/relay-key/'+encodeURIComponent(id),null,'DELETE');$('revoke-pair-id').value='';notice('Parringen er tilbagekaldt.');});
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());
async function init(){
  notice('');
  if(guestMatch){$('workspace').hidden=false;$('login').hidden=true;$('admin-actions').hidden=true;$('shares-section').hidden=true;$('root').hidden=true;$('library-eyebrow').textContent='DELT MED DIG';$('connection-state').textContent='Privat delingslink';if(!token){notice('Adgangsnøglen mangler. Åbn det fulde delingslink igen, inklusive delen efter #.');return;}await loadFiles();return;}
  try{await loadStatus();$('login').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;await loadFiles();}
  catch(error){if(error.status===401){$('login').hidden=false;$('workspace').hidden=true;}else throw error;}
}
window.addEventListener('beforeunload',event=>{if(busy){event.preventDefault();event.returnValue='';}});
init().catch(error=>notice(error.message));
