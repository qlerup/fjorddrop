"""Initialize private shared state, then drop privileges and exec Coturn."""
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import sys
import urllib.request

STATE = Path('/turn-state')


def address():
    explicit = os.getenv('TURN_PUBLIC_IP', '').strip()
    if explicit:
        return str(ipaddress.IPv4Address(explicit))
    # HTTPS detection is best-effort; a local install still starts without WAN.
    try:
        with urllib.request.urlopen('https://api.ipify.org', timeout=5) as result:
            ip = ipaddress.IPv4Address(result.read(64).decode().strip())
            if ip.is_global:
                return str(ip)
    except (OSError, ValueError):
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(('1.1.1.1', 80))
        return sock.getsockname()[0]


def options(host, secret, port, minimum, maximum):
    if not 1024 <= port <= 65535 or not 1024 <= minimum <= maximum <= 65535 or minimum <= port <= maximum:
        raise ValueError('TURN ports must be non-overlapping and between 1024 and 65535')
    ipaddress.IPv4Address(host)
    if len(secret) < 32 or not all(c.isalnum() or c in '-_' for c in secret):
        raise ValueError('Invalid TURN secret')
    return '\n'.join([
        'listening-ip=0.0.0.0', f'listening-port={port}', f'external-ip={host}',
        f'min-port={minimum}', f'max-port={maximum}',
        'realm=fjorddrop', 'server-name=fjorddrop', 'fingerprint', 'use-auth-secret',
        f'static-auth-secret={secret}', 'no-cli', 'no-tls', 'no-dtls', 'no-multicast-peers',
        'no-tcp-relay', 'stale-nonce=600', 'user-quota=32', 'total-quota=128',
        'denied-peer-ip=0.0.0.0-0.255.255.255',
        'denied-peer-ip=10.0.0.0-10.255.255.255',
        'denied-peer-ip=127.0.0.0-127.255.255.255',
        'denied-peer-ip=169.254.0.0-169.254.255.255',
        'denied-peer-ip=172.16.0.0-172.31.255.255',
        'denied-peer-ip=192.168.0.0-192.168.255.255',
        'denied-peer-ip=::1', 'denied-peer-ip=fc00::-fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff',
        'denied-peer-ip=fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff',
        'log-file=stdout', 'simple-log', 'pidfile=/tmp/turnserver.pid', 'userdb=/tmp/turn.sqlite',
    ]) + '\n'


def health():
    port = int(os.getenv('TURN_PORT', '3478'))
    transaction = secrets.token_bytes(12)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.sendto(struct.pack('!HHI', 1, 0, 0x2112A442) + transaction, ('127.0.0.1', port))
        response, _ = sock.recvfrom(2048)
    if len(response) < 20 or response[:2] != b'\x01\x01' or response[8:20] != transaction:
        raise ValueError('No valid STUN binding response')


def main():
    if '--health' in sys.argv:
        health()
        return
    uid, gid = int(os.getenv('PUID', '1000')), int(os.getenv('PGID', '1000'))
    if uid < 1 or gid < 1:
        raise ValueError('PUID and PGID must be non-root')
    STATE.mkdir(exist_ok=True)
    path = STATE / 'config.json'
    secret = json.loads(path.read_text())['secret'] if path.exists() else secrets.token_urlsafe(48)
    host = address()
    port, minimum, maximum = (int(os.getenv(k, v)) for k, v in
                              [('TURN_PORT', '3478'), ('TURN_MIN_PORT', '49160'), ('TURN_MAX_PORT', '49200')])
    conf = options(host, secret, port, minimum, maximum)
    relay_ip = os.getenv('TURN_RELAY_IP', '').strip()
    if relay_ip:
        conf += f'relay-ip={ipaddress.IPv4Address(relay_ip)}\n'
    tmp = STATE / 'config.new'
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump({'secret': secret, 'host': host, 'port': port, 'min_port': minimum,
                   'max_port': maximum, 'public': ipaddress.ip_address(host).is_global}, output)
    os.chown(tmp, uid, gid)
    os.replace(tmp, path)
    os.chown(STATE, uid, gid)
    os.chmod(STATE, 0o700)
    config_path = Path('/tmp/fjorddrop-turn.conf')
    fd = os.open(config_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(conf)
    os.chown(config_path, uid, gid)
    print(f'FjordDrop TURN: {host}:{port}; relay UDP {minimum}-{maximum}. Router forwarding is required behind NAT.', flush=True)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.execv('/usr/bin/turnserver', ['turnserver', '-c', str(config_path)])


if __name__ == '__main__':
    main()
