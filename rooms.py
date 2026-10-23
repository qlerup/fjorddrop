"""Two-browser rendezvous. Only signaling crosses HTTP; never file content."""
import base64
import json
import logging
import re
import secrets
import time

from aiohttp import web, ClientTimeout
from urllib.parse import urlsplit

END_REASONS = {
    'left': 'Den anden har forladt forbindelsen. Opret en ny kode.',
    'page_hidden': 'Den andens side blev lukket eller genindlæst. Hold begge sider åbne, og opret en ny kode.',
    'closed': 'Den anden browser lukkede forbindelsen uden en fejlårsag. Genindlæs begge sider, og opret en ny kode.',
    'connection_failed': 'Den anden kunne ikke opretholde filforbindelsen. Kontrollér VPN/TURN og netværksadgang på begge sider.',
    'vpn': 'VPN-forbindelsen var ikke klar. Filforbindelsen blev lukket for at undgå overførsel uden VPN. Opret en ny kode, når VPN er klar.',
    'inactive': 'En af browserne holdt op med at svare. Hold begge faner åbne, og opret en ny kode.',
    'expired': 'Forbindelseskoden er udløbet. Opret en ny kode.',
}


def valid_signal(signal):
    if not isinstance(signal, dict):
        return False
    if signal.get('type') in ('offer', 'answer'):
        return isinstance(signal.get('sdp'), str) and len(signal['sdp'])<=60000
    candidates = signal.get('candidates')
    if signal.get('type')!='candidates' or not isinstance(candidates,list) or not 1<=len(candidates)<=32:
        return False
    for candidate in candidates:
        if not isinstance(candidate,dict) or not isinstance(candidate.get('candidate'),str) or len(candidate['candidate'])>2048:
            return False
        for key,limit in (('sdpMid',128),('usernameFragment',256)):
            value=candidate.get(key)
            if value is not None and (not isinstance(value,str) or len(value)>limit):
                return False
        index=candidate.get('sdpMLineIndex')
        if index is not None and (type(index) is not int or not 0<=index<=16):
            return False
        if set(candidate)-{'candidate','sdpMid','sdpMLineIndex','usernameFragment'}:
            return False
    return True


class Rooms:
    def __init__(self, drop):
        self.drop = drop
        self.room = None
        self.last_closed = None
        self.proxy = None

    @property
    def active(self):
        r = self.room
        if r and time.time()>r['expires']:
            self.close('expired')
        elif r and any(time.time()-p['seen']>75 for p in r['people'].values()):
            self.close('inactive')
        if self.proxy and time.time()-self.proxy['seen']>75:
            self.close('inactive')
        return self.room is not None or self.proxy is not None

    def close(self, reason='closed', role='server'):
        if hasattr(self.drop,'received'):
            self.drop.received.clear_on_disconnect()
        proxy=self.proxy;self.proxy=None
        if proxy:
            self.drop.task(self.proxy_close(proxy,reason))
        if self.room:
            logging.getLogger('fjorddrop.rooms').warning('Room closed: reason=%s source=%s joined=%s signals=%s diagnostics=%s',
                reason if reason in END_REASONS else 'closed', role, 'guest' in self.room['people'],
                self.room.get('serial',0), {r:p.get('diagnostic',{}) for r,p in self.room['people'].items()})
            self.last_closed = {'id':self.room['id'], 'tokens':[p['token'] for p in self.room['people'].values()],
                                'reason':END_REASONS.get(reason, END_REASONS['closed']), 'expires':time.time()+120}
        self.room = None

    async def proxy_close(self, proxy, reason):
        try:await self.proxy_call(proxy,'room_close',{'reason':'connection_failed' if reason=='vpn' else 'left'},timeout=5)
        except Exception:logging.getLogger('fjorddrop.rooms').warning('Remote room close could not be delivered.')

    async def proxy_call(self, proxy, action, body, timeout=40):
        async with self.drop.http.post(proxy['endpoint']+'/'+action,json=body,
                headers={'Authorization':'Bearer '+proxy['remote_token']},allow_redirects=False,timeout=ClientTimeout(total=timeout)) as response:
            raw=bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                if len(raw)>128*1024:raise ValueError('Ugyldigt forbindelsessvar.')
            try:data=json.loads(raw)
            except ValueError:raise ValueError('Den anden FjordDrop kunne ikke kontaktes.') from None
            if not isinstance(data,dict):raise ValueError('Ugyldigt forbindelsessvar.')
            if response.status!=200:raise ValueError(str(data.get('error','Den anden FjordDrop svarede ikke.'))[:300])
            return data

    async def join(self, request):
        """Keep the receiver on its own origin, including HTTPS -> HTTP peers."""
        await self.drop.admin(request)
        body=await request.json()
        code=body.get('code','')
        if not isinstance(code,str) or not re.fullmatch(r'FD1\.[A-Za-z0-9_-]{40,2048}',code):
            raise ValueError('Ugyldig forbindelseskode.')
        try:
            encoded=code[4:];base,node,rid,token=json.loads(base64.urlsafe_b64decode(encoded+'='*((-len(encoded))%4)))
            url=urlsplit(base)
            if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
                raise ValueError()
            if not all(isinstance(v,str) and re.fullmatch('[A-Za-z0-9_-]{1,128}',v) for v in (node,rid,token)):raise ValueError()
        except (ValueError,TypeError):raise ValueError('Ugyldig forbindelseskode.') from None
        if self.room and rid==self.room['id']:
            info=await self.action(rid,token,'room_join',{})
            return web.json_response(dict(info,id=rid,node='local'))
        if self.active or self.drop.connection is not None:
            raise ValueError('Afbryd den aktuelle forbindelse først.')
        # Reserve before the first network await; only one peer may join.
        ice=self.drop.ice()
        proxy={'id':secrets.token_urlsafe(24),'token':secrets.token_urlsafe(24),'remote_token':token,
               'endpoint':base.rstrip('/')+'/api/guest/'+node+'/'+rid,'seen':time.time()}
        self.proxy=proxy
        try:
            info=await self.proxy_call(proxy,'room_join',{})
            proxy['remote_token']=info['token']
            if self.proxy is not proxy:
                await self.proxy_call(proxy,'room_close',{'reason':'left'})
                raise ValueError('Forbindelsen blev afbrudt.')
            return web.json_response({'id':proxy['id'],'node':'local','token':proxy['token'],
                'ice':ice or info['ice'],'force_relay':bool(self.drop.vpn.profile) or info.get('force_relay',False)})
        except Exception:
            if self.proxy is proxy:self.proxy=None
            raise

    async def create(self, request):
        await self.drop.admin(request)
        if self.active or self.drop.connection is not None:
            raise ValueError('Der er allerede en forbindelse. Afbryd den først.')
        ice = self.drop.ice()
        rid, invite, host = (secrets.token_urlsafe(24) for _ in range(3))
        now = time.time()
        self.room = {'id':rid, 'invite':invite, 'expires':now+86400,
                     'people':{'host':{'token':host,'seen':now,'queue':[]}}, 'serial':0}
        s = self.drop.settings()
        base = s['relay_url'] or s['public_url'] or str(request.url.origin())
        node = s['relay_id'] if s['relay_url'] else 'local'
        payload = json.dumps([base,node,rid,invite],separators=(',',':')).encode()
        code = 'FD1.'+base64.urlsafe_b64encode(payload).decode().rstrip('=')
        return web.json_response({'id':rid,'token':host,'code':code,'ice':ice,
                                  'force_relay':bool(self.drop.vpn.profile)})

    async def action(self, rid, token, action, body):
        if self.active and self.proxy and self.proxy['id']==rid:
            proxy=self.proxy
            if not secrets.compare_digest(token,proxy['token']):raise PermissionError('Ugyldig forbindelse.')
            if action not in ('room_poll','room_close'):raise ValueError('Ukendt handling.')
            proxy['seen']=time.time()
            if action=='room_close':
                self.proxy=None
                if hasattr(self.drop,'received'):await self.drop.received.cancel_all()
                return await self.proxy_call(proxy,action,body,timeout=5)
            return await self.proxy_call(proxy,action,body)
        if not self.active or not self.room or self.room['id']!=rid:
            closed = self.last_closed
            if closed and time.time()<closed['expires'] and closed['id']==rid and any(secrets.compare_digest(token,t) for t in closed['tokens']):
                raise ValueError(closed['reason'])
            raise ValueError('Forbindelsen er afsluttet. Opret en ny kode.')
        r = self.room
        if action=='room_join':
            if not secrets.compare_digest(token,r['invite']):
                raise PermissionError('Ugyldig forbindelseskode.')
            if 'guest' in r['people']:
                raise ValueError('Forbindelsen er optaget. Der er kun plads til to personer.')
            ice = self.drop.ice()
            key = secrets.token_urlsafe(24)
            r['people']['guest']={'token':key,'seen':time.time(),'queue':[]}
            return {'token':key,'ice':ice,'force_relay':bool(self.drop.vpn.profile)}
        role = next((role for role,p in r['people'].items() if secrets.compare_digest(token,p['token'])),None)
        if role is None:
            raise PermissionError('Ugyldig forbindelse.')
        p = r['people'][role]
        diagnostic = body.get('diagnostic', {})
        if isinstance(diagnostic, dict):
            clean = {}
            for key in ('connection','ice','gathering','signaling'):
                value = diagnostic.get(key)
                if value in ('new','checking','connecting','connected','completed','disconnected','failed','closed','gathering','complete','stable','have-local-offer','have-remote-offer'):
                    clean[key] = value
            for key in ('relay_candidates','ice_error'):
                value = diagnostic.get(key)
                if type(value) is int and 0<=value<=1000:
                    clean[key] = value
            if clean:
                p['diagnostic'] = clean
        if action=='room_close':
            reason = body.get('reason')
            self.close(reason if reason in ('left','connection_failed','page_hidden') else 'closed',role)
            if hasattr(self.drop,'received'):await self.drop.received.cancel_all()
            return {'ok':True}
        if action!='room_poll':
            raise ValueError('Ukendt handling.')
        p['seen']=time.time()
        ack=body.get('ack',0)
        if type(ack) is not int or ack<0 or ack>r['serial']:
            raise ValueError('Ugyldig kvittering.')
        p['queue']=[m for m in p['queue'] if m['id']>ack]
        other = r['people'].get('guest' if role=='host' else 'host')
        signal = body.get('signal')
        if signal is not None:
            if not valid_signal(signal):
                raise ValueError('Ugyldigt forbindelsessvar.')
            if not other or len(other['queue'])>=4:
                raise ValueError('Modtageren er ikke klar.')
            r['serial']+=1
            other['queue'].append({'id':r['serial'],'signal':signal})
        return {'joined':other is not None,'messages':p['queue']}


def routes(app, drop):
    app.router.add_post('/api/rooms', drop.rooms.create)
    app.router.add_post('/api/rooms/join', drop.rooms.join)
