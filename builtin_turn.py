"""Automatic Coturn discovery and short-lived REST credentials."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import time


def configuration():
    location = os.getenv('BUILTIN_TURN_STATE', '')
    if not location:
        return None
    try:
        return json.loads(Path(location).read_text())
    except (OSError, ValueError):
        return None


def ice_servers(now=None):
    config = configuration()
    if not config:
        return []
    username = str(int(time.time() if now is None else now) + 21600) + ':fjorddrop'
    credential = base64.b64encode(hmac.new(config['secret'].encode(), username.encode(), hashlib.sha1).digest()).decode()
    host, port = config['host'], config['port']
    return [
        {'urls': f'stun:{host}:{port}'},
        {'urls': f'turn:{host}:{port}?transport=udp', 'username': username, 'credential': credential},
        {'urls': f'turn:{host}:{port}?transport=tcp', 'username': username, 'credential': credential},
    ]


def public_status():
    config = configuration()
    if not config:
        return {'configured': False}
    return {key: config[key] for key in ('host', 'port', 'min_port', 'max_port', 'public')} | {'configured': True}
