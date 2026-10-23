"""Optional, owner-scoped FjordVPN relay lifecycle. Never restarts a VPN."""
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import time

OWNER = 'dk.fjorddrop.vpn-owner'


def bridge_network(container):
    network = container.attrs['NetworkSettings'].get('Networks', {}).get('bridge', {})
    for key in ('Gateway', 'IPAddress'):
        ipaddress.IPv4Address(network.get(key, ''))
    return network


async def docker_work(fn, *args):
    # Cancellation must not leave a thread mutating containers after the lock
    # has been released or shutdown has begun.
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class VPN:
    def __init__(self, drop, data):
        self.drop = drop
        self.folder = data / 'vpn'
        self.folder.mkdir(mode=0o700, exist_ok=True)
        self.owner = drop.store.get('vpn_owner') or secrets.token_hex(16)
        drop.store.set('vpn_owner', self.owner)
        self.profile = drop.store.get('vpn_profile')
        self.status = {'ready': False, 'message': 'FjordVPN er ikke valgt.', 'profiles': []}
        self.config = None
        self.client = None
        self.lock = asyncio.Lock()
        self.updated = 0
        path = self.folder / 'secret'
        if not path.exists():
            with path.open('x') as output:
                output.write(secrets.token_urlsafe(48))
            path.chmod(0o600)
        self.secret = path.read_text()

    def docker(self):
        if self.client is None:
            import docker
            self.client = docker.from_env(timeout=8)
        return self.client

    def discover(self):
        manager = self.docker().containers.get(os.getenv('FJORDVPN_CONTAINER', 'fjordvpn'))
        if manager.status != 'running' or manager.labels.get('com.docker.compose.service') != 'app':
            raise ValueError('Start FjordVPN i FjordHub først.')
        result = manager.exec_run(['python', '/app/seedbox_export.py'])
        if result.exit_code:
            raise ValueError('Opdatér FjordVPN for at vise forbindelserne.')
        data = json.loads(result.output)
        profiles = data['profiles']
        if data.get('version') != 1 or not isinstance(profiles, list) or len(profiles) > 20:
            raise ValueError('Ugyldigt svar fra FjordVPN.')
        for p in profiles:
            if not re.fullmatch('[a-f0-9]{32}', p['id']):
                raise ValueError('Ugyldig VPN-profil.')
        return profiles

    def children(self):
        return self.docker().containers.list(all=True, filters={'label': OWNER+'='+self.owner})

    def firewall(self, vpn, port, add):
        # Only our dedicated ports and comment; no flush, policy or tunnel changes.
        gateway = bridge_network(vpn)['Gateway']
        ipaddress.IPv4Address(gateway)
        rules = [
            ['-i', 'tun0', '-p', 'udp', '--dport', f'{port}:{port+32}'],
            ['-i', 'tun0', '-p', 'tcp', '--dport', str(port)],
            ['-i', 'eth0', '-s', gateway+'/32', '-p', 'tcp', '--dport', str(port)],
        ]
        for rule in rules:
            args = rule+['-m', 'comment', '--comment', 'fjorddrop-'+self.owner, '-j', 'ACCEPT']
            result = vpn.exec_run(['iptables', '-C', 'INPUT', *args])
            if result.exit_code not in (0, 1):
                raise ValueError('VPN-firewall kunne ikke kontrolleres.')
            if (add and result.exit_code == 1) or (not add and result.exit_code == 0):
                result = vpn.exec_run(['iptables', '-I' if add else '-D', 'INPUT', *args])
                if result.exit_code:
                    raise ValueError('VPN-firewall kunne ikke opdateres.')

    def remove(self, child):
        import docker
        child.stop(timeout=8)
        try:
            vpn = self.docker().containers.get(child.labels['dk.fjorddrop.vpn-container'])
            if vpn.status == 'running':
                self.firewall(vpn, int(child.labels['dk.fjorddrop.vpn-port']), False)
        except docker.errors.NotFound:
            pass
        child.remove()

    def reconcile(self, profile):
        profiles = self.discover() if profile else []
        selected = next((p for p in profiles if p['id'] == profile), None)
        children = self.children()
        if not profile:
            for c in children:
                self.remove(c)
            return None, profiles
        status = (selected or {}).get('status', {})
        if not selected or not selected.get('desired') or not status.get('healthy') or time.time()-status.get('checked_at', 0)>30:
            for c in children:
                self.remove(c)
            raise ValueError('Afventer den valgte VPN. Filoverførsler er blokeret.')
        # Host networking inherits the host's hostname, not the container ID.
        parent = self.docker().containers.get(os.getenv('FJORDDROP_CONTAINER', 'fjorddrop'))
        if parent.labels.get('dk.fjorddrop.app') != '1':
            raise ValueError('FjordDrop-containeren kunne ikke identificeres. Opdatér installationen.')
        vpn = self.docker().containers.get('fjordvpn-'+profile+'-vpn')
        if vpn.labels.get('dk.fjordvpn.managed') != '1' or vpn.labels.get('dk.fjordvpn.profile') != profile:
            raise ValueError('VPN-containerens ejerskab kunne ikke bekræftes.')
        if vpn.attrs['State'].get('Health', {}).get('Status') != 'healthy':
            raise ValueError('VPN er ikke klar. Filoverførsler er blokeret.')
        host = str(ipaddress.IPv4Address(status['public_ip']))
        own = next((c for c in children if c.labels.get('dk.fjorddrop.vpn-container') == vpn.id
                    and c.labels.get('dk.fjorddrop.vpn-ip') == host
                    and c.attrs['Image'] == parent.attrs['Image']
                    and c.attrs['State']['StartedAt'] >= vpn.attrs['State']['StartedAt']), None)
        for c in children:
            if c != own:
                self.remove(c)
        if own is None:
            source = next(m['Source'] for m in parent.attrs['Mounts'] if m['Destination']=='/data' and m['Type']=='bind')
            result = vpn.exec_run(['ip', '-4', '-o', 'addr', 'show', 'dev', 'tun0'])
            match = re.search(rb'inet (\d+\.\d+\.\d+\.\d+)/', result.output)
            if result.exit_code or not match:
                raise ValueError('VPN-interface mangler.')
            relay_ip = str(ipaddress.IPv4Address(match[1].decode()))
            # Dedicated block, never the gateway/torrent or game forwarding ports.
            port = 60000 + int(self.owner[:4],16)%100 * 33
            labels = {OWNER:self.owner, 'dk.fjorddrop.vpn-container':vpn.id,
                      'dk.fjorddrop.vpn-port':str(port), 'dk.fjorddrop.vpn-ip':host}
            project = parent.labels.get('com.docker.compose.project')
            if not project:
                raise ValueError('FjordVPN kræver installation med Docker Compose.')
            labels.update({'com.docker.compose.project':project, 'com.docker.compose.service':'vpn-relay',
                           'com.docker.compose.oneoff':'False', 'com.docker.compose.config-hash':'fjorddrop-vpn-v1'})
            own = self.docker().containers.create(parent.attrs['Image'], name='fjorddrop-'+self.owner+'-vpn',
                command=['/usr/local/bin/fjorddrop-vpn-turn'], labels=labels, network_mode='container:'+vpn.id,
                user=f'{os.getuid()}:{os.getgid()}',
                environment={'PUBLIC_IP':host, 'LISTEN_PORT':str(port), 'RELAY_IP':relay_ip},
                volumes={str(Path(source)/'vpn'):{'bind':'/state','mode':'rw'}},
                read_only=True, cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
                mem_limit='128m', pids_limit=64, restart_policy={'Name':'no'})
            (self.folder/'status.json').unlink(missing_ok=True)
        port = int(own.labels['dk.fjorddrop.vpn-port'])
        self.firewall(vpn, port, True)
        if own.status != 'running':
            own.start()
        try:
            config = json.loads((self.folder/'status.json').read_text())
            if time.time()-config['checked_at']>35 or config['host']!=host:
                raise ValueError()
        except (OSError, ValueError):
            raise ValueError('Opretter krypteret forbindelse og VPN-porte …') from None
        config['local_host'] = bridge_network(vpn)['IPAddress']
        return config, profiles

    async def refresh(self):
        async with self.lock:
            if not self.profile and not self.client:
                return
            try:
                self.config, profiles = await docker_work(self.reconcile, self.profile)
                self.updated = time.monotonic()
                self.status = {'ready': bool(self.config), 'profiles': profiles,
                    'message': 'Filoverførsler går gennem FjordVPN.' if self.config else 'FjordVPN er ikke valgt.'}
            except Exception as exc:
                self.config = None
                self.status.update(ready=False, message=str(exc) if isinstance(exc, ValueError)
                    else 'FjordVPN kan ikke kontaktes. Kontrollér Docker-adgang og at FjordVPN kører.')
            if self.profile and not self.config:
                await self.drop.close_transfers()

    async def run(self):
        while True:
            await self.refresh()
            await asyncio.sleep(3)

    async def select(self, profile):
        if not isinstance(profile, str) or (profile and not re.fullmatch('[a-f0-9]{32}', profile)):
            raise ValueError('Vælg en gyldig FjordVPN-forbindelse.')
        async with self.lock:
            if profile:
                profiles = await asyncio.to_thread(self.discover)
                if not any(p['id']==profile for p in profiles):
                    raise ValueError('VPN-forbindelsen findes ikke længere.')
            await self.drop.close_transfers()
            self.profile = profile
            self.config = None
            self.drop.store.set('vpn_profile', profile)
        await self.refresh()

    def ice(self, local=False):
        if not self.config or time.monotonic()-self.updated>15:
            raise ValueError('FjordVPN er ikke klar. Filoverførsler er blokeret.')
        cfg = self.config
        username = str(int(time.time())+21600)+':fjorddrop'
        password = base64.b64encode(hmac.new(self.secret.encode(),username.encode(),hashlib.sha1).digest()).decode()
        host, port = (cfg['local_host'],cfg['local_port']) if local else (cfg['host'],cfg['port'])
        # The app reaches the relay over its Docker bridge, then all file bytes
        # leave through tun0. TCP avoids an extra locally exposed UDP listener.
        return [{'urls':f'turn:{host}:{port}?transport=tcp', 'username':username,'credential':password}]

    async def stop(self):
        if self.client:
            async with self.lock:
                await docker_work(self.reconcile, '')


def force_relay(pc):
    """aiortc 1.15 lacks a public iceTransportPolicy; pin and test this adapter."""
    from aioice import TransportPolicy
    if pc.sctp is None:
        raise ValueError('Forbindelsen indeholder ingen filkanal.')
    pc.sctp.transport.transport.iceGatherer._connection._transport_policy = TransportPolicy.RELAY
