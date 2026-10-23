"""Server-side receiver: remote HTTP(S) signaling never crosses browser mixed-content rules."""
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import ClientTimeout, web
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription
from transfer import unpack
from vpn import force_relay


def parse_link(link):
    url = urlsplit(str(link))
    match = re.fullmatch(r'/s/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9_-]{1,64})', url.path)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or not match or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', url.fragment):
        raise ValueError('Indsæt det komplette FjordDrop-link med adgangsnøglen efter #.')
    return f'{url.scheme}://{url.netloc}/api/guest/{match[1]}/{match[2]}', url.fragment


async def call(drop, link, action, body):
    base, token = parse_link(link)
    async with drop.http.post(base+'/'+action, json=body, headers={'Authorization': 'Bearer '+token},
                              allow_redirects=False, timeout=ClientTimeout(total=40)) as response:
        raw = await response.content.read(2*1024*1024+1)
        if len(raw)>2*1024*1024 or response.status!=200:
            raise ValueError('Den anden FjordDrop svarede ikke korrekt. Kontrollér linket og at afsenderen er online.')
        result=json.loads(raw)
        if not isinstance(result,dict):
            raise ValueError('Ugyldigt svar fra afsenderen.')
        return result


class Remote:
    def __init__(self, drop):
        self.drop=drop
        self.jobs={}

    async def browse(self, request):
        await self.drop.admin(request)
        body=await request.json()
        return web.json_response(await call(self.drop, body['url'], 'browse', {'path':body.get('path','')}))

    async def offer(self, request):
        await self.drop.admin(request)
        body=await request.json()
        return web.json_response(await call(self.drop, body['url'], 'offer', {'sdp':body['sdp']}))

    async def start(self, request):
        await self.drop.admin(request)
        if self.drop.connection is not None or self.drop.rooms.active or any(j['state']=='running' for j in self.jobs.values()):
            raise ValueError('En forbindelse er allerede aktiv. Afbryd den først.')
        body=await request.json()
        parse_link(body['url'])
        jid=secrets.token_urlsafe(12)
        while len(self.jobs)>=50:
            victim=next((key for key,value in self.jobs.items() if value['state']!='running'),None)
            if victim is None:break
            self.jobs.pop(victim)
        job={'id':jid,'state':'running','bytes':0,'size':0,'name':'Forbinder','started':time.time()}
        self.jobs[jid]=job
        job['task']=self.drop.task(self.receive(body['url'],str(body['path']),job))
        return web.json_response({'id':jid})

    async def status(self, request):
        await self.drop.admin(request)
        job=self.jobs.get(request.match_info['jid'])
        if not job:raise web.HTTPNotFound()
        if request.method=='DELETE':
            job['task'].cancel()
            await asyncio.gather(job['task'],return_exceptions=True)
        return web.json_response({k:v for k,v in job.items() if k!='task'})

    async def receive(self, link, path, job):
        pc=None
        partial=None
        output=None
        try:
            info=await call(self.drop, link, 'browse', {})
            ice=self.drop.ice(local=True) or info.get('ice',[])
            pc=RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(**i) for i in ice]))
            self.drop.claim_connection(pc)
            dc=pc.createDataChannel('fjorddrop-v1',ordered=True)
            if self.drop.vpn.profile:
                force_relay(pc)
            opened=asyncio.Event()
            queue=asyncio.Queue(maxsize=64)
            @dc.on('open')
            def on_open():opened.set()
            @dc.on('message')
            def on_message(message):
                if len(message)>32768 or queue.full():
                    dc.close()
                else:queue.put_nowait(message)
            await pc.setLocalDescription(await pc.createOffer())
            answer=await call(self.drop, link, 'offer', {'sdp':pc.localDescription.sdp})
            await pc.setRemoteDescription(RTCSessionDescription(**answer))
            await asyncio.wait_for(opened.wait(),35)
            dc.send(json.dumps({'op':'get','path':path}))
            index=0
            while True:
                message=await asyncio.wait_for(queue.get(),60)
                if isinstance(message,bytes):
                    if output is None:raise ValueError('Filmetadata mangler.')
                    payload=unpack(message,index)
                    if not payload or job['bytes']+len(payload)>job['size']:raise ValueError('Forkert filstørrelse.')
                    await asyncio.to_thread(output.write,payload)
                    index+=1
                    job['bytes']+=len(payload)
                    if index%16==0:dc.send(json.dumps({'op':'pull'}))
                    continue
                data=json.loads(message)
                if data.get('op')=='error':raise ValueError(str(data.get('error','Overførslen fejlede.'))[:200])
                if data.get('op')=='meta':
                    name=data['name'];size=data['size']
                    if output is not None or not isinstance(name,str) or not name or len(name)>180 or name.startswith('.') or any(c in name for c in '/\\:\x00') or any(ord(c)<32 for c in name):raise ValueError('Ugyldigt filnavn.')
                    if type(size)is not int or not 0<=size<=self.drop.max_bytes:raise ValueError('Filen overskrider den konfigurerede størrelsesgrænse.')
                    import shutil
                    if shutil.disk_usage(self.drop.roots['received']).free<size+64*1024*1024:raise ValueError('Der er ikke nok ledig plads.')
                    destination=self.drop.roots['received']/('download-'+job['id'])
                    destination.mkdir()
                    final=destination/name
                    partial=destination/'.partial'
                    output=partial.open('xb')
                    job.update(name=name,size=size)
                    dc.send(json.dumps({'op':'pull'}))
                elif data.get('op')=='done':
                    if output is None or data.get('bytes')!=job['bytes'] or job['bytes']!=job['size']:raise ValueError('Filen er ikke komplet.')
                    await asyncio.to_thread(output.flush)
                    await asyncio.to_thread(os.fsync,output.fileno())
                    output.close();output=None
                    os.replace(partial,final);partial=None
                    job.update(state='complete',path=final.relative_to(self.drop.roots['received']).as_posix())
                    break
                else:raise ValueError('Ugyldigt svar fra afsenderen.')
        except asyncio.CancelledError:
            job.update(state='cancelled',error='Overførslen blev annulleret.')
        except Exception as exc:
            job.update(state='error',error=str(exc)[:200] if isinstance(exc,ValueError) else 'Overførslen mislykkedes. Kontrollér afsender, diskplads og STUN/TURN.')
        finally:
            if output:output.close()
            if partial:partial.unlink(missing_ok=True)
            if pc:await pc.close()


def routes(app,drop):
    remote=Remote(drop)
    drop.remote=remote
    app['remote']=remote
    app.add_routes([web.post('/api/remote/browse',remote.browse),web.post('/api/remote/offer',remote.offer),web.post('/api/remote/download',remote.start),
                    web.get('/api/jobs/{jid}',remote.status),web.delete('/api/jobs/{jid}',remote.status)])
