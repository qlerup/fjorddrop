'use strict';
// Only catalogue messages are sent when a file is added. Bytes require an
// explicit GET, and every window of 16 chunks requires receiver credit.
class RoomPeer {
  constructor(ice, forceRelay, callbacks) {
    this.pc=new RTCPeerConnection({iceServers:ice,iceTransportPolicy:forceRelay?'relay':'all'});
    this.callbacks=callbacks;this.files=new Map();this.remote=[];this.closed=false;this.chain=Promise.resolve();this.sending=null;this.receiving=null;this.bytesSent=0;
    this.pc.onicecandidateerror=e=>{this.iceError=e.errorCode;};
    this.candidates=[];
    this.pc.onicecandidate=e=>{if(e.candidate)this.candidates.push(e.candidate.toJSON());};
    this.pc.onconnectionstatechange=()=>{if(['failed','closed'].includes(this.pc.connectionState))this.close('Forbindelsen blev afbrudt.');};
    this.pc.ondatachannel=e=>this.channel(e.channel);
    this.watchdog=setInterval(()=>{for(const job of [this.sending,this.receiving])if(job?.touched&&Date.now()-job.touched>60000)this.close('Overførslen svarede ikke i 60 sekunder. Start filen igen.');},5000);
  }
  diagnostic(){return {connection:this.pc.connectionState,ice:this.pc.iceConnectionState,gathering:this.pc.iceGatheringState,signaling:this.pc.signalingState,relay_candidates:(this.pc.localDescription?.sdp.match(/ typ relay/g)||[]).length,ice_error:this.iceError};}
  channel(dc){
    if(this.dc||dc.label!=='fjorddrop-room-v1'||!dc.ordered){dc.close();return;}
    this.dc=dc;dc.binaryType='arraybuffer';dc.onopen=()=>{this.callbacks.open();this.catalogue();};
    dc.onmessage=e=>{this.chain=this.chain.then(()=>this.message(e.data)).catch(e=>this.close(e.message));};
    dc.onclose=()=>this.close('Forbindelsen blev afsluttet.');
  }
  async description(type){
    if(type==='offer')this.channel(this.pc.createDataChannel('fjorddrop-room-v1',{ordered:true}));
    await this.pc.setLocalDescription(type==='offer'?await this.pc.createOffer():await this.pc.createAnswer());
    const sdp=this.pc.localDescription.sdp;
    // Exchange SDP immediately. ICE gathering can continue after a usable
    // relay exists; waiting for "complete" prevented the answer being sent.
    return {type:this.pc.localDescription.type,sdp};
  }
  send(message){if(this.dc?.readyState!=='open')throw new Error('Forbindelsen er ikke klar.');this.dc.send(JSON.stringify(message));}
  catalogue(){if(this.dc?.readyState==='open')this.send({op:'files',files:[...this.files].map(([id,f])=>({id,name:f.name,size:f.size}))});}
  add(files){
    if(this.files.size+files.length>200)throw new Error('Du kan lægge op til 200 filer klar ad gangen.');
    for(const f of files){if(!(f instanceof File)||f.name.length>240)throw new Error('Vælg filer med et gyldigt navn.');const id=Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');this.files.set(id,f);}
    this.catalogue();this.callbacks.local(this.files);
  }
  remove(id){if(this.sending?.id===id)throw new Error('Vent til filen er hentet færdig.');this.files.delete(id);this.catalogue();this.callbacks.local(this.files);}
  async message(data){
    if(typeof data==='string'){
      if(data.length>100000)throw new Error('Ugyldig besked.');
      const m=JSON.parse(data);
      if(m.op==='files'){
        if(!Array.isArray(m.files)||m.files.length>200||m.files.some(f=>!/^[a-f0-9]{32}$/.test(f.id)||typeof f.name!=='string'||f.name.length>240||!Number.isSafeInteger(f.size)||f.size<0))throw new Error('Ugyldig filoversigt.');
        this.remote=m.files;this.callbacks.remote(m.files);
      }else if(m.op==='get'){
        const file=this.files.get(m.id);if(!file||this.sending){this.send({op:'unavailable'});return;}
        this.sending={id:m.id,file,offset:0,index:0,touched:Date.now()};this.send({op:'meta',id:m.id,size:file.size});
      }else if(m.op==='meta'){
        if(!this.receiving||m.id!==this.receiving.id||m.size!==this.receiving.size)throw new Error('Filen ændrede sig.');
        this.send({op:'pull'});
      }else if(m.op==='pull'){
        const job=this.sending;if(!job)return;job.touched=Date.now();
        for(let n=0;n<16&&job.offset<job.file.size;n++){
          const bytes=await job.file.slice(job.offset,job.offset+16384).arrayBuffer();const frame=new Uint8Array(bytes.byteLength+36);
          new DataView(frame.buffer).setUint32(0,job.index++);frame.set(new Uint8Array(sha256.arrayBuffer(bytes)),4);frame.set(new Uint8Array(bytes),36);
          this.dc.send(frame);job.offset+=bytes.byteLength;this.bytesSent+=bytes.byteLength;
        }
        this.callbacks.progress('Sender',job.file.name,job.offset,job.file.size);
        if(job.offset===job.file.size){this.send({op:'done',id:job.id,size:job.offset});this.sending=null;}
      }else if(m.op==='done'){
        const j=this.receiving;if(!j||m.id!==j.id||m.size!==j.size||j.bytes!==j.size)throw new Error('Filen er ikke komplet.');
        if(j.writer)await j.writer.close();else{const url=URL.createObjectURL(new Blob(j.parts));const a=document.createElement('a');a.href=url;a.download=j.name;a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);}
        this.receiving=null;this.callbacks.progress('Gemt',j.name,j.size,j.size);this.callbacks.remote(this.remote);
      }else if(m.op==='unavailable'){
        if(this.receiving?.writer)await this.receiving.writer.abort();this.receiving=null;this.callbacks.error('Filen er fjernet, eller afsenderen sender en anden fil. Prøv igen.');
      }else throw new Error('Ukendt filbesked.');
      return;
    }
    const j=this.receiving;
    if(!j||!(data instanceof ArrayBuffer)||data.byteLength<=36||data.byteLength>16420)throw new Error('Ugyldig filbid.');
    const view=new DataView(data),payload=data.slice(36),hash=new Uint8Array(sha256.arrayBuffer(payload));
    if(view.getUint32(0)!==j.index||!hash.every((b,i)=>b===view.getUint8(i+4))||j.bytes+payload.byteLength>j.size)throw new Error('Filkontrollen fejlede. Filen gemmes ikke.');
    if(j.writer)await j.writer.write(payload);else j.parts.push(payload);
    j.index++;j.bytes+=payload.byteLength;j.touched=Date.now();this.callbacks.progress('Henter',j.name,j.bytes,j.size);
    if(j.index%16===0&&j.bytes<j.size)this.send({op:'pull'});
  }
  async download(file){
    if(this.receiving)throw new Error('Vent til den aktuelle fil er hentet.');
    // Reserve before awaiting the picker: a double click cannot start two jobs.
    const j={...file,bytes:0,index:0,parts:[],writer:null};this.receiving=j;
    try{
      if(window.showSaveFilePicker){const handle=await showSaveFilePicker({suggestedName:file.name});j.writer=await handle.createWritable();}
      else if(file.size>256*1024*1024)throw new Error('Filer over 256 MB kræver Chrome/Edge via HTTPS med valg af gemmested.');
      j.touched=Date.now();this.send({op:'get',id:file.id});
    }catch(e){this.receiving=null;if(j.writer)await j.writer.abort();throw e;}
  }
  close(reason){if(this.closed)return;this.closed=true;clearInterval(this.watchdog);this.receiving?.writer?.abort().catch(()=>{});this.receiving=null;this.sending=null;this.files.clear();this.dc?.close();this.pc.close();this.callbacks.closed(reason);}
}
