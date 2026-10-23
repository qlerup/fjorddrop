/* WebRTC endpoint. Memory is bounded by one window unless Blob fallback is used. */
'use strict';
const PIECE=16384, WINDOW=16, BLOB_LIMIT=256*1024*1024;
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function hashBytes(buffer){return new Uint8Array(sha256.arrayBuffer(buffer));}
class FilePeer {
  constructor(api, ice, progress){this.api=api;this.ice=ice;this.progress=progress;this.pc=null;this.dc=null;this.waiters=[];this.job=null;this.chain=Promise.resolve();this.closed=false;}
  async connect(){
    this.pc=new RTCPeerConnection({iceServers:this.ice});
    this.dc=this.pc.createDataChannel('fjorddrop-v1',{ordered:true});this.dc.binaryType='arraybuffer';
    this.dc.onmessage=e=>{this.chain=this.chain.then(()=>this.message(e.data)).catch(error=>this.fail(error));};
    this.dc.onclose=()=>{if(!this.closed)this.fail(new Error('Forbindelsen blev afbrudt. Start filen igen.'));};
    this.pc.onconnectionstatechange=()=>{if(this.pc.connectionState==='failed')this.fail(new Error('Ingen forbindelse. Kontrollér STUN/TURN på afsenderens FjordDrop.'));};
    const opened=new Promise((resolve,reject)=>{this.openReject=reject;this.dc.onopen=resolve;});
    // Attach rejection handler before ICE negotiation finishes.
    opened.catch(()=>{});
    try{
      await this.pc.setLocalDescription(await this.pc.createOffer());
      if(this.pc.iceGatheringState!=='complete')await Promise.race([new Promise(resolve=>{this.pc.addEventListener('icegatheringstatechange',()=>{if(this.pc.iceGatheringState==='complete')resolve();});}),delay(15000)]);
      const answer=await this.api('offer',{sdp:this.pc.localDescription.sdp});
      await this.pc.setRemoteDescription(answer);
      let timer;try{await Promise.race([opened,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Forbindelsen kunne ikke oprettes. Konfigurér TURN for dette netværk.')),30000);})]);}finally{clearTimeout(timer);}
      const stats=await this.pc.getStats();let relay=false;
      stats.forEach(s=>{if(s.type==='candidate-pair'&&s.state==='succeeded'){const a=stats.get(s.localCandidateId),b=stats.get(s.remoteCandidateId);if(a?.candidateType==='relay'||b?.candidateType==='relay')relay=true;}});
      return relay?'Krypteret via TURN-relæ':'Direkte · krypteret WebRTC';
    }catch(error){this.close();throw error;}
  }
  send(data){if(this.closed||this.dc?.readyState!=='open')throw new Error('Forbindelsen er lukket.');this.dc.send(JSON.stringify(data));}
  wait(op){return new Promise((resolve,reject)=>{const waiter={op,resolve,reject};waiter.timer=setTimeout(()=>{this.waiters=this.waiters.filter(w=>w!==waiter);reject(new Error('Overførslen svarede ikke i 60 sekunder.'));},60000);this.waiters.push(waiter);});}
  async message(data){
    for(const waiter of this.waiters){clearTimeout(waiter.timer);waiter.timer=setTimeout(()=>this.fail(new Error('Overførslen svarede ikke i 60 sekunder.')),60000);}
    if(typeof data==='string'){
      const msg=JSON.parse(data);if(msg.op==='error')throw new Error(msg.error);
      if(msg.op==='ack'){this.acked=msg.bytes;this.progress(msg.bytes,this.job.size);}
      if(msg.op==='meta'){
        if(!this.job||this.job.direction!=='download'||msg.size!==this.job.size)throw new Error('Filen ændrede sig. Opdatér oversigten.');
        this.send({op:'pull'});
      }
      if(msg.op==='done'){
        const j=this.job;if(!j||j.received!==j.size||msg.bytes!==j.size)throw new Error('Filen er ikke komplet.');
        if(j.writer)await j.writer.close();
        else{const url=URL.createObjectURL(new Blob(j.parts));const a=document.createElement('a');a.href=url;a.download=j.name;a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);}
        this.progress(j.size,j.size);this.job=null;
      }
      for(const w of [...this.waiters])if(w.op===msg.op){clearTimeout(w.timer);this.waiters=this.waiters.filter(v=>v!==w);w.resolve(msg);}
      return;
    }
    const j=this.job;if(!j||j.direction!=='download'||data.byteLength<36||data.byteLength>PIECE+36)throw new Error('Ugyldig filbid.');
    const view=new DataView(data),payload=data.slice(36),hash=await hashBytes(payload);
    if(view.getUint32(0)!==j.index||!hash.every((b,i)=>b===view.getUint8(4+i))||j.received+payload.byteLength>j.size)throw new Error('Integritetskontrol fejlede. Filen gemmes ikke.');
    if(j.writer)await j.writer.write(payload);else j.parts.push(payload);
    j.received+=payload.byteLength;j.index++;this.progress(j.received,j.size);
    if(j.index%WINDOW===0)this.send({op:'pull'});
  }
  async download(file,writer){
    if(!writer&&file.size>BLOB_LIMIT)throw new Error('Filer over 256 MB kræver Chrome/Edge via HTTPS med valg af gemmested.');
    this.job={direction:'download',name:file.name,size:file.size,writer,parts:[],received:0,index:0};
    const done=this.wait('done');this.send({op:'get',path:file.path});await done;
  }
  async upload(file){
    this.job={direction:'upload',size:file.size};this.acked=0;
    const ready=this.wait('ready');this.send({op:'put',name:file.name,size:file.size});await ready;
    let index=0;
    for(let start=0;start<file.size;start+=PIECE){
      if(this.closed)throw new Error('Overførslen er annulleret.');
      const payload=await file.slice(start,start+PIECE).arrayBuffer();const hash=await hashBytes(payload);
      const frame=new Uint8Array(36+payload.byteLength);new DataView(frame.buffer).setUint32(0,index++);frame.set(hash,4);frame.set(new Uint8Array(payload),36);
      this.dc.send(frame);
      if(index%WINDOW===0||start+payload.byteLength===file.size){
        const target=start+payload.byteLength,deadline=Date.now()+60000;
        while(this.acked<target){if(this.closed)throw new Error('Forbindelsen er lukket.');if(Date.now()>deadline)throw new Error('Modtageren svarede ikke.');await delay(5);}
      }
    }
    const saved=this.wait('saved');this.send({op:'finish'});const result=await saved;this.job=null;return result;
  }
  fail(error){this.openReject?.(error);for(const w of this.waiters){clearTimeout(w.timer);w.reject(error);}this.waiters=[];this.error=error;this.close();}
  close(){if(this.closed)return;this.closed=true;if(this.job?.writer)this.job.writer.abort().catch(()=>{});for(const w of this.waiters){clearTimeout(w.timer);w.reject(new Error('Overførsel afbrudt.'));}this.waiters=[];this.dc?.close();this.pc?.close();}
}
