"""Bounded browser-to-local-disk staging for HTTP receivers."""
import asyncio
import os
import re
import secrets
import shutil
import time

from aiohttp import web
from storage import confined
from transfer import CHUNK, WINDOW, unpack


class Received:
    def __init__(self, drop):
        self.drop=drop
        self.jobs={}
        self.completed={}
        self.lock=asyncio.Lock()
        self.root=drop.roots['received']/'.fjorddrop-room-cache'
        self.root.mkdir(mode=0o700,exist_ok=True)
        if self.root.is_symlink():raise ValueError('Den midlertidige mappe må ikke være et symbolsk link.')
        # Clean only this application's private temporary area after a restart.
        for folder in self.root.iterdir():
            if folder.is_dir() and not folder.is_symlink() and re.fullmatch('[a-f0-9]{32}',folder.name):
                self.remove_folder(folder)

    def remove_folder(self, folder):
        for path in folder.iterdir():
            if path.is_file() or path.is_symlink():path.unlink()
        folder.rmdir()

    async def start(self, request):
        await self.drop.admin(request)
        body=await request.json()
        name,size=body.get('name'),body.get('size')
        if not isinstance(name,str) or not name or len(name)>180 or name.startswith('.') or any(c in name for c in '/\\:\x00') or any(ord(c)<32 for c in name):
            raise ValueError('Ugyldigt filnavn.')
        if type(size) is not int or not 0<=size<=self.drop.max_bytes:
            raise ValueError('Filen overskrider serverens størrelsesgrænse.')
        async with self.lock:
            if not self.drop.rooms.active:
                raise ValueError('Forbind til den anden først.')
            if self.jobs:
                raise ValueError('Vent til den aktuelle fil er gemt på serveren.')
            root=self.root
            if shutil.disk_usage(root).free<size+64*1024*1024:
                raise ValueError('Der er ikke plads til filen på modtagerens server.')
            ident=secrets.token_hex(16)
            folder=root/ident;folder.mkdir(mode=0o700)
            output=(folder/'.partial').open('xb')
            self.jobs[ident]={'folder':folder,'name':name,'size':size,'bytes':0,'index':0,'output':output,'seen':time.monotonic()}
        return web.json_response({'id':ident})

    async def job(self, request):
        await self.drop.admin(request)
        ident=request.match_info['ident']
        # Read at most one protocol window; never buffer the whole file.
        raw=await request.clone(client_max_size=WINDOW*(CHUNK+36)+1).read() if request.method=='POST' else b''
        async with self.lock:
            job=self.jobs.get(ident)
            if not job:raise web.HTTPNotFound()
            if request.method=='DELETE':
                self.abort(ident)
                return web.json_response({'ok':True})
            if request.match_info.get('action')=='complete':
                if raw!=b'{}' or job['bytes']!=job['size']:
                    raise ValueError('Filen er ikke komplet.')
                def finish():
                    job['output'].flush();os.fsync(job['output'].fileno());job['output'].close()
                    os.replace(job['folder']/'.partial',job['folder']/job['name'])
                await asyncio.to_thread(finish)
                self.jobs.pop(ident)
                self.completed[ident]=job['folder']
                return web.json_response({'url':'/api/received/'+ident})
            try:
                offset=0;index=job['index'];count=job['bytes'];payloads=[]
                while offset<len(raw):
                    length=min(CHUNK,job['size']-count)
                    if not length:raise ValueError('Forkert filstørrelse.')
                    frame=raw[offset:offset+length+36]
                    if len(frame)!=length+36:raise ValueError('Ufuldstændig filbid.')
                    payloads.append(unpack(frame,index));index+=1;count+=length;offset+=length+36
                if not payloads:raise ValueError('Filbid mangler.')
                await asyncio.to_thread(job['output'].write,b''.join(payloads))
                job.update(index=index,bytes=count,seen=time.monotonic())
            except Exception:
                self.abort(ident)
                raise
            return web.json_response({'bytes':job['bytes']})

    def abort(self, ident):
        job=self.jobs.pop(ident,None)
        if job:
            job['output'].close();(job['folder']/'.partial').unlink(missing_ok=True)
            job['folder'].rmdir()

    async def cancel_all(self):
        await self.cancel_ids(list(self.jobs)+list(self.completed))

    def clear_on_disconnect(self):
        identifiers=list(self.jobs)+list(self.completed)
        if identifiers:self.drop.task(self.cancel_ids(identifiers))

    async def cancel_ids(self, identifiers):
        async with self.lock:
            for ident in identifiers:
                self.abort(ident)
                folder=self.completed.pop(ident,None)
                if folder:self.remove_folder(folder)

    async def download(self, request):
        await self.drop.admin(request)
        ident=request.match_info['ident']
        if not re.fullmatch('[a-f0-9]{32}',ident):raise web.HTTPNotFound()
        if ident not in self.completed:raise web.HTTPNotFound()
        folder=confined(self.root,ident,directory=True)
        files=[p for p in folder.iterdir() if not p.name.startswith('.') and p.is_file() and not p.is_symlink()]
        if len(files)!=1:raise web.HTTPNotFound()
        from urllib.parse import quote
        return web.FileResponse(files[0],headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(files[0].name,safe=''),'Cache-Control':'no-store'})

    async def lifecycle(self, app):
        async def sweep():
            while True:
                await asyncio.sleep(15)
                # Also expire orphaned sessions when a browser disappears
                # without delivering pagehide or another poll.
                _ = self.drop.rooms.active
                async with self.lock:
                    for ident,job in list(self.jobs.items()):
                        if time.monotonic()-job['seen']>120:self.abort(ident)
        task=asyncio.create_task(sweep())
        yield
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        await self.cancel_all()


def routes(app, drop):
    receiver=Received(drop);drop.received=receiver
    app.cleanup_ctx.append(receiver.lifecycle)
    app.add_routes([web.post('/api/received',receiver.start),web.post('/api/received/{ident}/chunks',receiver.job,name='received-chunks'),
                    web.post('/api/received/{ident}/{action:complete}',receiver.job),web.delete('/api/received/{ident}',receiver.job),
                    web.get('/api/received/{ident}',receiver.download)])
