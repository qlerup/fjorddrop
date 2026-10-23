import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path

import pytest
import builtin_turn
from app import create_app

spec = importlib.util.spec_from_file_location('turn_start', Path(__file__).resolve().parents[1]/'turn/start.py')
turn_start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(turn_start)


def test_generated_credentials_and_private_status(tmp_path, monkeypatch):
    path = tmp_path/'config.json'
    secret = 'test-secret-'*5
    path.write_text(json.dumps({'host':'203.0.113.8','port':3478,'min_port':49160,'max_port':49200,'public':True,'secret':secret}))
    monkeypatch.setenv('BUILTIN_TURN_STATE',str(path))
    servers = builtin_turn.ice_servers(now=1000)
    assert servers[0]['urls']=='stun:203.0.113.8:3478'
    assert servers[1]['username']=='22600:fjorddrop'
    expected=base64.b64encode(hmac.new(secret.encode(),b'22600:fjorddrop',hashlib.sha1).digest()).decode()
    assert servers[1]['credential']==expected
    assert servers[2]['urls'].endswith('?transport=tcp')
    assert 'secret' not in builtin_turn.public_status()
    drop=create_app(tmp_path/'data',tmp_path/'shared',tmp_path/'received')['drop']
    assert drop.ice()[1]['urls'].startswith('turn:203.0.113.8:3478')
    drop.store.set('turn_url','turns:existing.example:5349')
    drop.store.set('turn_username','existing')
    drop.store.set('turn_password','preserved')
    assert drop.ice()[-1]=={'urls':'turns:existing.example:5349','username':'existing','credential':'preserved'}
    drop.store.db.close()


def test_secret_is_required_and_ports_validated():
    config=turn_start.options('203.0.113.9','x'*48,3478,49160,49200)
    assert 'use-auth-secret\n' in config
    assert 'no-auth' not in config
    assert 'no-cli' in config and 'denied-peer-ip=169.254.' in config
    assert 'no-tcp-relay' in config  # TCP client transport remains enabled.
    for args in [('x'*48,3478,3400,3500),('x'*48,80,49160,49200),('x'*48,3478,50000,49000),('bad\nno-auth',3478,49160,49200)]:
        with pytest.raises(ValueError):turn_start.options('203.0.113.9',*args)


def test_bad_public_ip_fails_closed(monkeypatch):
    monkeypatch.setenv('TURN_PUBLIC_IP','1.2.3.4\nno-auth')
    with pytest.raises(ValueError):turn_start.address()


def test_missing_state_is_not_advertised(tmp_path,monkeypatch):
    monkeypatch.setenv('BUILTIN_TURN_STATE',str(tmp_path/'missing'))
    assert builtin_turn.ice_servers()==[]
    assert builtin_turn.public_status()=={'configured':False}
