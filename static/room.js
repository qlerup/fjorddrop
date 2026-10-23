'use strict';
const el=id=>document.getElementById(id);
let roomCSRF='',roomStatus=null,session=null,roomPeer=null,pollTimer=null,pollBusy=false;
let vpnTimer=null,vpnRevision=0,pendingJoin='';
function vpnFeedback(state,text){
  const banner=el('room-vpn-banner');banner.hidden=false;banner.dataset.state=state;banner.textContent=text;
  el('room-vpn-status').dataset.state=state;el('room-vpn-status').textContent=text;
}
function renderVPN(v){
  if(!v.profile_id){el('room-vpn-banner').hidden=true;el('room-vpn-status').dataset.state='off';el('room-vpn-status').textContent=v.message;return;}
  const name=v.profiles?.find(p=>p.id===v.profile_id)?.name||'FjordVPN';
  if(v.ready)vpnFeedback('ready',`✓ VPN klar · ${name}. Klar til at oprette en krypteret filforbindelse.`);
  else vpnFeedback('pending',`VPN er ikke klar endnu · ${v.message}`);
}
function watchVPN(revision,delay=3000){
  clearTimeout(vpnTimer);
  vpnTimer=setTimeout(async()=>{
    try{const result=await request('/api/status');if(revision!==vpnRevision)return;renderVPN(result.vpn);watchVPN(revision,result.vpn.ready?10000:3000);}
    catch(e){if(revision!==vpnRevision)return;vpnFeedback('error','VPN-status kunne ikke kontrolleres. Forbindelsen er ikke bekræftet klar.');if(e.status!==401)watchVPN(revision,5000);}
  },delay);
}
function message(text){el('room-notice').textContent=text;el('room-notice').hidden=!text;}
function guarded(fn){return async e=>{try{await fn(e);}catch(error){if(error.name!=='AbortError')message(error.message);}};}
async function request(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-CSRF-Token':roomCSRF},body:body?JSON.stringify(body):undefined});const text=await r.text();let data;try{data=JSON.parse(text);}catch{data={error:text};}if(!r.ok)throw Object.assign(new Error(data.error||'Forbindelsen svarede ikke.'),{status:r.status});return data;}
function size(n){if(n<1024)return n+' B';const i=Math.min(4,Math.floor(Math.log(n)/Math.log(1024)));return (n/1024**i).toFixed(1)+' '+['B','KB','MB','GB','TB'][i];}
function row(name,bytes,label,action){const r=document.createElement('div');r.className='room-file';const text=document.createElement('div'),title=document.createElement('strong'),detail=document.createElement('small');title.textContent=name;detail.textContent=size(bytes);text.append(title,detail);const b=document.createElement('button');b.className='secondary';b.textContent=label;b.onclick=guarded(action);r.append(text,b);return r;}
function showLocal(files){el('room-local-files').replaceChildren(...[...files].map(([id,f])=>row(f.name,f.size,'Fjern',()=>roomPeer.remove(id))));}
function showRemote(files){el('room-remote-files').replaceChildren(...files.map(f=>row(f.name,f.size,'Hent til pc',()=>roomPeer.download(f))));if(!files.length)el('room-remote-files').textContent='Den anden har ikke lagt filer klar endnu.';}
function decodeCode(value){
  const code=value.trim();if(!/^FD1\.[A-Za-z0-9_-]{40,2048}$/.test(code))throw new Error('Indsæt hele forbindelseskoden, som starter med FD1.');
  const encoded=code.slice(4);let parsed;try{parsed=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(encoded.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-encoded.length%4)%4)),c=>c.charCodeAt(0))));}catch{throw new Error('Koden kunne ikke læses.');}
  if(!Array.isArray(parsed)||parsed.length!==4)throw new Error('Ugyldig kode.');const [base,node,id,token]=parsed,url=new URL(base);
  if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.pathname!=='/'||url.search||url.hash||![node,id,token].every(x=>typeof x==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(x)))throw new Error('Ugyldig kode.');
  return {base:url.origin,node,id,token,code};
}
async function call(action,body={}){
  const s=session;if(!s)throw new Error('Forbindelsen er afsluttet.');
  const r=await fetch(`/api/guest/${s.node}/${s.id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+s.token},body:JSON.stringify(body)});
  const data=await r.json();if(!r.ok)throw new Error(data.error||'Forbindelsen er afsluttet.');return data;
}
function end(reason,code='left'){
  if(session)call('room_close',{reason:code,diagnostic:roomPeer?.diagnostic()}).catch(()=>{});
  document.body.classList.remove('room-connected');
  clearInterval(pollTimer);pollTimer=null;const pc=roomPeer;roomPeer=null;session=null;pc?.close();
  el('room-start').hidden=false;el('room-session').hidden=true;el('room-file-input').value='';showLocal(new Map());showRemote([]);if(reason)message(reason);
}
async function serverWriter(file){
  if(!roomCSRF)throw new Error('På HTTP skal du forbinde fra din egen FjordDrop og være logget ind, så filen kan gemmes midlertidigt på din server.');
  const job=await request('/api/received',{name:file.name,size:file.size});
  let frames=[],count=0,aborted=false;
  async function flush(){
    if(!frames.length)return;
    const batch=frames;frames=[];
    const r=await fetch(`/api/received/${job.id}/chunks`,{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':roomCSRF},body:new Blob(batch)});
    if(!r.ok){let result;try{result=await r.json();}catch{}throw new Error(result?.error||'Filen kunne ikke gemmes på serveren.');}
  }
  return {
    async write(payload,frame){if(aborted)throw new Error('Overførslen er afbrudt.');frames.push(frame);count+=payload.byteLength;if(frames.length===16||count===file.size)await flush();},
    async close(){await flush();const result=await request(`/api/received/${job.id}/complete`,{});const link=document.createElement('a');link.href=result.url;link.download=file.name;document.body.append(link);link.click();link.remove();},
    async abort(){aborted=true;frames=[];await fetch(`/api/received/${job.id}`,{method:'DELETE',headers:{'X-CSRF-Token':roomCSRF}});}
  };
}
function start(info,role){
  el('room-save-info').textContent=location.protocol==='http:'?'Download gemmer først filen midlertidigt på din FjordDrop-server og starter derefter download til pc’en. Serverkopien slettes, når forbindelsen afbrydes.':'I Chrome/Edge kan du gemme direkte på pc’en. Hold forbindelsen åben, indtil filen er hentet.';
  el('room-start').hidden=true;el('room-session').hidden=false;el('room-files').hidden=true;el('room-invite').hidden=role!=='host';el('room-transfer').hidden=true;el('room-state').textContent=role==='host'?'Venter på den anden …':'Forbinder krypteret …';
  roomPeer=new RoomPeer(info.ice,info.force_relay,{
    open(){document.body.classList.add('room-connected');el('room-state').textContent=info.force_relay?'Forbundet · krypteret gennem FjordVPN':'Forbundet · krypteret WebRTC';el('room-invite').hidden=true;el('room-files').hidden=false;},
    local:showLocal,remote:showRemote,error:message,serverWriter,closed:reason=>{if(session)end(reason||'Forbindelsen er afsluttet.','connection_failed');},
    progress(direction,name,done,total){el('room-transfer').hidden=false;el('room-transfer-label').textContent=direction+': '+name;el('room-progress').value=total?done/total*100:100;el('room-transfer-detail').textContent=size(done)+' / '+size(total);}
  });
  session.role=role;session.ack=0;session.offered=false;session.outgoing=[];session.started=Date.now();
  message('');
  pollTimer=setInterval(()=>poll().catch(e=>end(e.message,'connection_failed')),1000);poll().catch(e=>end(e.message,'connection_failed'));
}
async function poll(){
  if(pollBusy||!session)return;pollBusy=true;const current=session;
  try{
    // Descriptions must precede their candidates on the ordered signaling queue.
    const signal=current.outgoing.shift()||(roomPeer.candidates.length?{type:'candidates',candidates:roomPeer.candidates.splice(0,32)}:undefined);
    const reply=await call('room_poll',{ack:current.ack,diagnostic:roomPeer.diagnostic(),signal});if(session!==current)return;
    for(const item of reply.messages){
      if(item.id<=current.ack)continue;
      if(item.signal.type==='candidates'){
        for(const candidate of item.signal.candidates){await roomPeer.pc.addIceCandidate(candidate);if(session!==current)return;}
      }else{
        await roomPeer.pc.setRemoteDescription(item.signal);if(session!==current)return;
        if(item.signal.type==='offer'){const answer=await roomPeer.description('answer');if(session!==current)return;current.outgoing.push(answer);}
      }
      current.ack=item.id;
    }
    if(reply.joined&&!current.negotiatingAt){current.negotiatingAt=Date.now();el('room-state').textContent='Den anden er med · opretter krypteret filforbindelse …';el('room-invite').hidden=true;}
    if(reply.joined&&current.role==='host'&&!current.offered){current.offered=true;const offer=await roomPeer.description('offer');if(session!==current)return;current.outgoing.push(offer);}
    if(current.negotiatingAt&&roomPeer.pc.connectionState!=='connected'&&Date.now()-current.negotiatingAt>60000)throw new Error('Forbindelsen kunne ikke oprettes. Kontrollér VPN/TURN, og opret en ny kode.');
  }finally{pollBusy=false;}
}
async function join(code){
  if(session)throw new Error('Afbryd den nuværende forbindelse først.');const decoded=decodeCode(code);
  if(!roomCSRF)await account();
  if(roomCSRF){const info=await request('/api/rooms/join',{code:decoded.code});session={node:info.node,id:info.id,token:info.token};start(info,'guest');return;}
  if(decoded.base!==location.origin&&location.protocol==='http:'){pendingJoin=decoded.code;el('room-login').hidden=false;el('room-create').hidden=true;message('Log ind på din FjordDrop, så filer kan gemmes midlertidigt på din server.');el('room-login').elements.password.focus();return;}
  if(decoded.base!==location.origin){location.assign(decoded.base+'/connect#'+decoded.code);return;}
  session={node:decoded.node,id:decoded.id,token:decoded.token};
  try{const info=await call('room_join');session.token=info.token;start(info,'guest');}catch(e){session=null;throw e;}
}
el('room-join').onsubmit=guarded(async e=>{e.preventDefault();message('');await join(el('room-code-input').value);});
el('room-create').onclick=guarded(async()=>{
  if(session)return;message('');let info;
  try{info=await request('/api/rooms',{});}catch(e){if(e.status===401){el('room-login').hidden=false;el('room-create').hidden=true;el('room-login').elements.password.focus();return;}throw e;}
  session={id:info.id,token:info.token,node:'local',code:info.code};el('room-code').value=info.code;start(info,'host');
});
el('room-login').onsubmit=guarded(async e=>{e.preventDefault();await request('/api/login',Object.fromEntries(new FormData(e.target)));e.target.elements.password.value='';await account();el('room-login').hidden=true;el('room-create').hidden=false;if(pendingJoin){const code=pendingJoin;pendingJoin='';await join(code);}else el('room-create').click();});
el('room-leave').onclick=guarded(async()=>{try{await call('room_close',{reason:'left',diagnostic:roomPeer?.diagnostic()});}finally{end('Forbindelsen er afsluttet. Filerne på din pc er bevaret.');}});
async function copy(text){if(navigator.clipboard)await navigator.clipboard.writeText(text);else{const field=el('room-code'),previous=field.value;field.value=text;field.focus();field.select();const copied=document.execCommand('copy');field.value=previous;if(!copied)throw new Error('Markér og kopiér koden manuelt.');}message('Kopieret.');}
el('room-copy').onclick=guarded(()=>copy(session.code));el('room-copy-link').onclick=guarded(()=>{const c=decodeCode(session.code);return copy(c.base+'/connect#'+session.code);});
el('room-file-input').onchange=guarded(e=>{roomPeer.add([...e.target.files]);e.target.value='';});
for(const name of ['dragenter','dragover'])el('room-drop').addEventListener(name,e=>{e.preventDefault();el('room-drop').classList.add('dragging');});
for(const name of ['dragleave','drop'])el('room-drop').addEventListener(name,e=>{e.preventDefault();el('room-drop').classList.remove('dragging');});
el('room-drop').addEventListener('drop',guarded(e=>{const entries=[...e.dataTransfer.items].map(i=>i.webkitGetAsEntry?.());if(entries.some(x=>x?.isDirectory))throw new Error('Vælg filer. Mapper kan pakkes som ZIP først.');roomPeer.add([...e.dataTransfer.files]);}));
async function account(){
  try{roomStatus=await request('/api/status');roomCSRF=roomStatus.csrf;el('room-vpn').hidden=false;await loadVPN();}catch(e){if(e.status!==401)message(e.message);}
}
async function loadVPN(){const revision=++vpnRevision;clearTimeout(vpnTimer);const v=await request('/api/vpn');if(revision!==vpnRevision)return;const select=el('room-vpn-profile');select.replaceChildren(new Option('Uden FjordVPN · direkte / indbygget TURN',''));for(const p of v.profiles)select.add(new Option(p.name+(p.status?.healthy?' · klar':' · offline'),p.id));if(v.profile_id&&![...select.options].some(o=>o.value===v.profile_id))select.add(new Option('Valgt VPN · ikke tilgængelig',v.profile_id));select.value=v.profile_id;renderVPN(v);watchVPN(revision);}
el('room-vpn-save').onclick=guarded(async()=>{
  const revision=++vpnRevision;clearTimeout(vpnTimer);
  const controls=[el('room-vpn-save'),el('room-create'),el('room-join').querySelector('button')];controls.forEach(b=>b.disabled=true);
  vpnFeedback('pending',el('room-vpn-profile').value?'Forbinder til VPN … venter på at relæ og porte er klar.':'Slår FjordVPN fra …');message('');
  try{const result=await request('/api/vpn',{profile_id:el('room-vpn-profile').value});end();renderVPN(result);watchVPN(revision);}
  catch(e){vpnFeedback('error','VPN-valget kunne ikke gemmes. '+e.message);watchVPN(revision,5000);throw e;}
  finally{controls.forEach(b=>b.disabled=false);}
});
window.addEventListener('beforeunload',e=>{if(session){e.preventDefault();e.returnValue='';}});
window.addEventListener('pagehide',()=>{if(session)fetch(`/api/guest/${session.node}/${session.id}/room_close`,{method:'POST',keepalive:true,headers:{'Content-Type':'application/json','Authorization':'Bearer '+session.token},body:JSON.stringify({reason:'page_hidden',diagnostic:roomPeer?.diagnostic()})}).catch(()=>{});});
const invitation=location.hash.slice(1);if(invitation){history.replaceState(null,'',location.pathname);guarded(()=>join(invitation))();}else account();
