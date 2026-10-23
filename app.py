"""FjordDrop: self-hosted WebRTC peers, capability links and outbound rendezvous."""
import asyncio
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import sys
from urllib.parse import urlsplit

from aiohttp import web, ClientSession, ClientTimeout, WSMsgType
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer

from storage import Store, confined, listing, digest
from transfer import Transfer
from remote import routes as remote_routes
import builtin_turn

BASE = Path(__file__).parent


@web.middleware
async def errors(request, handler):
    try:
        if request.method == 'POST' and request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType(text='Brug application/json.')
        response = await handler(request)
    except PermissionError as exc:
        response = web.json_response({'error': str(exc)}, status=403)
    except (ValueError, KeyError, TypeError):
        response = web.json_response({'error': 'Ugyldig forespørgsel. Kontrollér felterne og prøv igen.'}, status=400)
    except web.HTTPException as exc:
        response = web.Response(status=exc.status, text=exc.text, headers=exc.headers)
    response.headers.update({'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
        'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
    return response


class Drop:
    def __init__(self, data, shared, received):
        self.store = Store(data)
        self.roots = {'shared': shared.resolve(), 'received': received.resolve()}
        for p in self.roots.values():
            p.mkdir(parents=True, exist_ok=True)
        self.sessions = {}
        self.login_attempts = deque(maxlen=100)
        self.peers = {}
        self.relays = {}
        self.pending = {}
        self.tasks = set()
        self.relay_status = 'Ikke parret'
        self.hub_url = os.getenv('FJORDHUB_URL', '').rstrip('/')
        self.hub_key = os.getenv('FJORDHUB_API_KEY', '')
        self.max_bytes = int(os.getenv('MAX_UPLOAD_GB', '100'))*1024**3
        self.http = None

    def task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def settings(self):
        return {k: self.store.get(k, os.getenv(k.upper(), '')) for k in
                ('public_url', 'relay_url', 'relay_id', 'relay_key', 'stun_url', 'turn_url', 'turn_username', 'turn_password')}

    def ice(self):
        s = self.settings()
        if not s['turn_url']:
            return builtin_turn.ice_servers() or ([{'urls': s['stun_url']}] if s['stun_url'] else [])
        ice = []
        if s['stun_url']:
            ice.append({'urls': s['stun_url']})
        if s['turn_url']:
            ice.append({'urls': s['turn_url'], 'username': s['turn_username'], 'credential': s['turn_password']})
        return ice

    async def hub(self, path, payload, method='POST'):
        if not self.hub_url or not self.hub_key:
            raise PermissionError('FjordHub-login er ikke konfigureret.')
        kwargs = {'params' if method == 'GET' else 'json': dict(payload, app_id='fjorddrop')}
        try:
            async with self.http.request(method, self.hub_url+path, headers={'X-Hub-Key': self.hub_key},
                                         allow_redirects=False, timeout=ClientTimeout(total=8), **kwargs) as response:
                result = await response.json()
                if response.status != 200 or result.get('ok') is not True:
                    raise PermissionError('FjordHub kunne ikke godkende login. Åbn FjordHub og kontrollér din adgang.')
                return result
        except (OSError, asyncio.TimeoutError):
            raise web.HTTPServiceUnavailable(text='FjordHub kan ikke kontaktes.') from None

    async def admin(self, request):
        sid = request.cookies.get('fjorddrop', '')
        session = self.sessions.get(sid)
        if not session or session['expires'] < time.time():
            self.sessions.pop(sid, None)
            raise web.HTTPUnauthorized()
        if self.hub_url:
            users = (await self.hub('/api/hub/apps/users', {}, 'GET')).get('items', [])
            user = next((u for u in users if u.get('id') == session['uid']), {})
            self.require_admin(user)
        if request.method not in ('GET', 'HEAD') and not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), session['csrf']):
            raise PermissionError('Genindlæs siden før du fortsætter.')
        return session

    @staticmethod
    def require_admin(user):
        if not user.get('id') or not (user.get('role') == 'admin' or user.get('hub_role') == 'admin'):
            raise PermissionError('Kun FjordHub-administratorer kan administrere delinger.')
        if user.get('must_change_password') or user.get('email_required'):
            raise PermissionError('Gør først din konto færdig i FjordHub.')

    def cookie(self, request, uid=0):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        # Single-process, bounded and intentionally invalidated on restart.
        self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > time.time()}
        if len(self.sessions) >= 256:
            self.sessions.pop(next(iter(self.sessions)))
        self.sessions[token] = {'uid': uid, 'csrf': csrf, 'expires': time.time()+12*3600}
        response = web.json_response({'csrf': csrf})
        secure = request.secure or request.headers.get('X-Forwarded-Proto', '').lower() == 'https'
        response.set_cookie('fjorddrop', token, httponly=True, samesite='Strict', secure=secure, max_age=43200)
        return response

    async def login(self, request):
        now = time.monotonic()
        if sum(t > now-60 for t in self.login_attempts) >= 15:
            raise web.HTTPTooManyRequests(text='Vent et minut før næste loginforsøg.')
        self.login_attempts.append(now)
        body = await request.json()
        if self.hub_url:
            user = (await self.hub('/api/hub/apps/authenticate', {'username': body.get('username', ''), 'password': body.get('password', '')})).get('user', {})
            self.require_admin(user)
            return self.cookie(request, user['id'])
        password = os.getenv('ADMIN_PASSWORD', '')
        if len(password) < 16:
            raise PermissionError('Angiv ADMIN_PASSWORD med mindst 16 tegn før standalone-login.')
        if not secrets.compare_digest(body.get('password', ''), password):
            raise PermissionError('Forkert adgangskode.')
        return self.cookie(request)

    async def sso(self, request):
        user = await self.hub('/api/hub/sso-verify', {'token': request.query.get('token', '')}, 'GET')
        self.require_admin(user)
        response = self.cookie(request, user['id'])
        response.set_status(303)
        response.headers['Location'] = '/'
        return response

    async def status(self, request):
        session = await self.admin(request)
        s = self.settings()
        return web.json_response({'csrf': session['csrf'], 'shares': self.store.shares(),
            'settings': {k: v for k, v in s.items() if k not in ('relay_key', 'turn_password')},
            'relay_key_set': bool(s['relay_key']), 'turn_password_set': bool(s['turn_password']),
            'relay_status': self.relay_status, 'peers': len(self.peers), 'max_upload': self.max_bytes,
            'destinations': {'shared': '/shared', 'received': '/received'},
            'builtin_turn': builtin_turn.public_status(), 'external_turn': bool(s['turn_url'])})

    async def logout(self, request):
        await self.admin(request)
        self.sessions.pop(request.cookies.get('fjorddrop', ''), None)
        response = web.json_response({'ok': True})
        response.del_cookie('fjorddrop')
        return response

    async def files(self, request):
        await self.admin(request)
        root = self.roots[request.query.get('root', 'shared')]
        return web.json_response({'files': await asyncio.to_thread(listing, root, request.query.get('path', ''))})

    async def create_share(self, request):
        await self.admin(request)
        body = await request.json()
        root, path = body.get('root', 'shared'), body.get('path', '')
        confined(self.roots[root], path, directory=True)
        hours = int(body.get('hours', 24))
        if hours not in (1, 24, 168, 720) or type(body.get('upload', False)) is not bool:
            raise ValueError()
        sid, token = self.store.create(str(body.get('name') or 'Min deling'), root, path, body.get('upload', False), hours)
        s = self.settings()
        base = s['relay_url'] or s['public_url'] or str(request.url.origin())
        node = s['relay_id'] if s['relay_url'] else 'local'
        return web.json_response({'id': sid, 'url': f'{base.rstrip("/")}/s/{node}/{sid}#{token}'})

    async def revoke(self, request):
        await self.admin(request)
        sid = request.match_info['sid']
        self.store.revoke(sid)
        for pc, share_id in list(self.peers.items()):
            if share_id == sid:
                await pc.close()
        return web.json_response({'ok': True})

    async def save_settings(self, request):
        await self.admin(request)
        body = await request.json()
        values = {}
        for key in self.settings():
            if key not in body:
                continue
            value = str(body[key]).strip()
            if len(value) > 1024:
                raise ValueError()
            if key in ('public_url', 'relay_url') and value:
                parsed = urlsplit(value)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                    raise ValueError('Brug en HTTPS-adresse uden sti.')
                value = value.rstrip('/')
            if key == 'stun_url' and value and not value.startswith('stun:'):
                raise ValueError()
            if key == 'turn_url' and value and not value.startswith(('turn:', 'turns:')):
                raise ValueError()
            values[key] = value
        for key, value in values.items():
            self.store.set(key, value)
        return web.json_response({'ok': True})

    async def relay_key(self, request):
        await self.admin(request)
        rid, key = self.store.relay_key()
        return web.json_response({'id': rid, 'key': key, 'url': self.settings()['public_url']})

    async def relay_revoke(self, request):
        await self.admin(request)
        rid = request.match_info['rid']
        self.store.db.execute('DELETE FROM relays WHERE id=?', (rid,))
        self.store.db.commit()
        ws = self.relays.get(rid)
        if ws:
            await ws.close()
        return web.json_response({'ok': True})

    async def relay_socket(self, request):
        rid = request.match_info['rid']
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not self.store.relay_valid(rid, token):
            raise PermissionError('Ugyldig parring.')
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=256*1024)
        await ws.prepare(request)
        previous = self.relays.get(rid)
        if previous:
            await previous.close()
        self.relays[rid] = ws
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    data = json.loads(message.data)
                    future = self.pending.get((rid, data.get('id')))
                    if future and not future.done():
                        future.set_result(data)
        finally:
            if self.relays.get(rid) is ws:
                self.relays.pop(rid, None)
        return ws

    async def dispatch(self, sid, token, action, body):
        share = self.store.share(sid, token)
        root = confined(self.roots[share['root']], share['path'], directory=True)
        if action == 'browse':
            return {'name': share['name'], 'upload': bool(share['upload']), 'expires': share['expires'],
                    'ice': self.ice(), 'max_upload': self.max_bytes,
                    'files': await asyncio.to_thread(listing, root, body.get('path', ''))}
        if action != 'offer' or len(self.peers) >= 16:
            raise ValueError('Der er for mange forbindelser. Prøv igen senere.')
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(**i) for i in self.ice()]))
        self.peers[pc] = sid

        @pc.on('connectionstatechange')
        async def state():
            if pc.connectionState in ('failed', 'closed'):
                self.peers.pop(pc, None)
                if pc.connectionState != 'closed':
                    await pc.close()

        channels = []
        @pc.on('datachannel')
        def channel(dc):
            if channels or dc.label != 'fjorddrop-v1' or not dc.ordered:
                dc.close()
                return
            channels.append(Transfer(dc, root, self.roots['received']/sid,
                                     lambda: self.store.share(sid, token), self.max_bytes))

        async def timeout():
            await asyncio.sleep(45)
            if pc.connectionState != 'connected':
                await pc.close()
                self.peers.pop(pc, None)
            else:
                while pc.connectionState == 'connected':
                    await asyncio.sleep(30)
                    try:
                        self.store.share(sid, token)
                    except PermissionError:
                        break
                    if not channels or channels[0].task.done():
                        break
                await pc.close()
                self.peers.pop(pc, None)
        self.task(timeout())
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=body['sdp'], type='offer'))
            await pc.setLocalDescription(await pc.createAnswer())
            return {'sdp': pc.localDescription.sdp, 'type': pc.localDescription.type}
        except Exception:
            await pc.close()
            self.peers.pop(pc, None)
            raise

    async def guest(self, request):
        node, sid, action = (request.match_info[k] for k in ('node', 'sid', 'action'))
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        body = await request.json()
        if action not in ('browse', 'offer') or len(token) > 128:
            raise ValueError()
        if node == 'local':
            return web.json_response(await self.dispatch(sid, token, action, body))
        ws = self.relays.get(node)
        if not ws or ws.closed:
            raise web.HTTPServiceUnavailable(text='Afsenderen er offline. Prøv igen, når FjordDrop kører.')
        if len(self.pending) >= 64:
            raise web.HTTPTooManyRequests()
        rid = secrets.token_urlsafe(12)
        future = asyncio.get_running_loop().create_future()
        self.pending[(node, rid)] = future
        try:
            await ws.send_json({'id': rid, 'sid': sid, 'token': token, 'action': action, 'body': body})
            result = await asyncio.wait_for(future, 35)
            return web.json_response(result.get('body', {}), status=result.get('status', 502))
        except asyncio.TimeoutError:
            raise web.HTTPGatewayTimeout(text='Afsenderen svarede ikke.') from None
        finally:
            self.pending.pop((node, rid), None)

    async def connector(self):
        while True:
            s = self.settings()
            if not all(s[k] for k in ('relay_url', 'relay_id', 'relay_key')):
                self.relay_status = 'Ikke parret'
                await asyncio.sleep(3)
                continue
            try:
                self.relay_status = 'Forbinder'
                url = s['relay_url'].replace('https://', 'wss://', 1)+'/api/relay/'+s['relay_id']
                async with self.http.ws_connect(url, headers={'Authorization': 'Bearer '+s['relay_key']}, heartbeat=20, max_msg_size=256*1024) as ws:
                    self.relay_status = 'Online'
                    async def check_config():
                        while not ws.closed:
                            await asyncio.sleep(3)
                            if self.settings() != s:
                                await ws.close()
                    watcher = asyncio.create_task(check_config())
                    calls = set()
                    async def handle(data):
                        try:
                            body = await self.dispatch(data['sid'], data['token'], data['action'], data['body'])
                            result = {'id': data['id'], 'status': 200, 'body': body}
                        except (PermissionError, ValueError, KeyError, TypeError):
                            result = {'id': data.get('id'), 'status': 403, 'body': {'error': 'Delingen er utilgængelig eller forespørgslen ugyldig.'}}
                        except Exception:
                            result = {'id': data.get('id'), 'status': 503, 'body': {'error': 'Forbindelsen kunne ikke oprettes.'}}
                        if not ws.closed:
                            await ws.send_json(result)
                    try:
                        async for message in ws:
                            if message.type == WSMsgType.TEXT and len(calls) < 32:
                                task = asyncio.create_task(handle(json.loads(message.data)))
                                calls.add(task)
                                task.add_done_callback(calls.discard)
                    finally:
                        watcher.cancel()
                        for task in calls:
                            task.cancel()
                        await asyncio.gather(watcher, *calls, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.relay_status = 'Offline – kontrollér adresse og parringsnøgle'
            await asyncio.sleep(5)


def create_app(data=None, shared=None, received=None):
    app = web.Application(middlewares=[errors], client_max_size=128*1024)
    drop = Drop(Path(data or os.getenv('DATA_DIR', 'data')), Path(shared or os.getenv('SHARED_DIR', 'shared')),
                Path(received or os.getenv('RECEIVED_DIR', 'received')))
    app['drop'] = drop
    async def lifecycle(app):
        drop.http = ClientSession(timeout=ClientTimeout(total=40))
        connector = asyncio.create_task(drop.connector())
        yield
        connector.cancel()
        for task in list(drop.tasks):
            task.cancel()
        await asyncio.gather(connector, *drop.tasks, return_exceptions=True)
        await asyncio.gather(*(pc.close() for pc in list(drop.peers)), return_exceptions=True)
        for ws in list(drop.relays.values()):
            await ws.close()
        await drop.http.close()
        drop.store.db.close()
    app.cleanup_ctx.append(lifecycle)
    async def index(request):
        return web.FileResponse(BASE/'static/index.html')
    async def health(request):
        return web.json_response({'ok': True, 'app': 'fjorddrop'})
    app.add_routes([
        web.get('/', index), web.get('/s/{node}/{sid}', index), web.get('/healthz', health),
        web.post('/api/login', drop.login), web.post('/api/logout', drop.logout),
        web.get('/sso', drop.sso), web.get('/auth/sso', drop.sso), web.get('/hub-login', drop.sso),
        web.get('/api/status', drop.status), web.get('/api/files', drop.files),
        web.post('/api/shares', drop.create_share), web.delete('/api/shares/{sid}', drop.revoke),
        web.post('/api/settings', drop.save_settings), web.post('/api/relay-key', drop.relay_key),
        web.delete('/api/relay-key/{rid}', drop.relay_revoke), web.get('/api/relay/{rid}', drop.relay_socket),
        web.post('/api/guest/{node}/{sid}/{action}', drop.guest),
        web.static('/static', BASE/'static')])
    remote_routes(app, drop)
    return app


if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    web.run_app(create_app(), host=os.getenv('BIND_ADDRESS', '0.0.0.0'), port=int(os.getenv('APP_PORT', '8102')), access_log=None)
