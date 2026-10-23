"""Two-browser rendezvous. Only signaling crosses HTTP; never file content."""
import base64
import json
import secrets
import time

from aiohttp import web

END_REASONS = {
    'left': 'Den anden har forladt forbindelsen. Opret en ny kode.',
    'connection_failed': 'Den anden kunne ikke opretholde filforbindelsen. Kontrollér VPN/TURN og netværksadgang på begge sider.',
    'vpn': 'VPN-forbindelsen var ikke klar. Filforbindelsen blev lukket for at undgå overførsel uden VPN. Opret en ny kode, når VPN er klar.',
    'inactive': 'En af browserne holdt op med at svare. Hold begge faner åbne, og opret en ny kode.',
    'expired': 'Forbindelseskoden er udløbet. Opret en ny kode.',
}


class Rooms:
    def __init__(self, drop):
        self.drop = drop
        self.room = None
        self.last_closed = None

    @property
    def active(self):
        r = self.room
        if r and time.time()>r['expires']:
            self.close('expired')
        elif r and any(time.time()-p['seen']>75 for p in r['people'].values()):
            self.close('inactive')
        return self.room is not None

    def close(self, reason='left'):
        if self.room:
            self.last_closed = {'id':self.room['id'], 'tokens':[p['token'] for p in self.room['people'].values()],
                                'reason':END_REASONS.get(reason, END_REASONS['left']), 'expires':time.time()+120}
        self.room = None

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
        if not self.active or self.room['id']!=rid:
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
        if action=='room_close':
            reason = body.get('reason')
            self.close(reason if reason in ('left','connection_failed') else 'left')
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
            if not isinstance(signal,dict) or signal.get('type') not in ('offer','answer') or not isinstance(signal.get('sdp'),str) or len(signal['sdp'])>60000:
                raise ValueError('Ugyldigt forbindelsessvar.')
            if not other or len(other['queue'])>=4:
                raise ValueError('Modtageren er ikke klar.')
            r['serial']+=1
            other['queue'].append({'id':r['serial'],'signal':signal})
        return {'joined':other is not None,'messages':p['queue']}


def routes(app, drop):
    app.router.add_post('/api/rooms', drop.rooms.create)
