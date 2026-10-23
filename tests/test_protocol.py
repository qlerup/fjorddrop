import asyncio
import hashlib
import os
from pathlib import Path
import time

import pytest
from aiohttp import web
from aiortc import RTCPeerConnection, RTCConfiguration, RTCSessionDescription

from app import create_app
from storage import confined
from transfer import pack, unpack, CHUNK


@pytest.fixture
async def client(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_PASSWORD','test-password-at-least-16')
    app=create_app(tmp_path/'data', tmp_path/'shared', tmp_path/'received')
    return await aiohttp_client(app)


async def login(client):
    response=await client.post('/api/login',json={'password':'test-password-at-least-16'})
    assert response.status==200
    return {'X-CSRF-Token':(await response.json())['csrf']}


def test_chunk_integrity_and_index():
    data=os.urandom(CHUNK)
    packet=pack(9,data)
    assert unpack(packet,9)==data
    for damaged,index in [(packet,8),(packet[:-1]+bytes([packet[-1]^1]),9),(b'',0),(b'x'*(CHUNK+37),0)]:
        with pytest.raises(ValueError):unpack(damaged,index)


@pytest.mark.parametrize('path',['../secret','.hidden','folder/../../secret','C:/secret','folder\\..\\secret'])
def test_path_escape(tmp_path,path):
    with pytest.raises(ValueError):confined(tmp_path,path)


async def test_auth_csrf_shares_and_revocation(client):
    assert (await client.get('/api/status')).status==401
    assert (await client.post('/api/login',json={'password':'wrong'})).status==403
    headers=await login(client)
    assert (await client.post('/api/shares',json={})).status==403
    response=await client.post('/api/shares',headers=headers,json={'name':'A <b>name</b>','upload':False,'hours':24})
    assert response.status==200
    result=await response.json();sid=result['id'];token=result['url'].split('#')[1]
    path=f'/api/guest/local/{sid}/browse'
    assert (await client.post(path,json={})).status==403
    response=await client.post(path,json={},headers={'Authorization':'Bearer '+token})
    assert response.status==200
    assert (await response.json())['name']=='A <b>name</b>'
    assert (await client.post(path,json={'path':'..'},headers={'Authorization':'Bearer '+token})).status==400
    assert (await client.delete('/api/shares/'+sid,headers=headers)).status==200
    assert (await client.post(path,json={},headers={'Authorization':'Bearer '+token})).status==403


async def connect(client,sid,token):
    pc=RTCPeerConnection(RTCConfiguration(iceServers=[]))
    dc=pc.createDataChannel('fjorddrop-v1',ordered=True)
    opened=asyncio.Event();queue=asyncio.Queue()
    dc.on('open',opened.set)
    dc.on('message',queue.put_nowait)
    await pc.setLocalDescription(await pc.createOffer())
    response=await client.post(f'/api/guest/local/{sid}/offer',headers={'Authorization':'Bearer '+token},json={'sdp':pc.localDescription.sdp})
    assert response.status==200,await response.text()
    await pc.setRemoteDescription(RTCSessionDescription(**await response.json()))
    await asyncio.wait_for(opened.wait(),10)
    return pc,dc,queue


async def next_json(queue):
    import json
    return json.loads(await asyncio.wait_for(queue.get(),10))


@pytest.mark.parametrize('size',[0,17,CHUNK*35+7])
async def test_real_webrtc_upload_download(client,size):
    import json
    drop=client.app['drop'];payload=os.urandom(size)
    (drop.roots['shared']/'source.bin').write_bytes(payload)
    sid,token=drop.store.create('files','shared','',True,24)
    pc,dc,q=await connect(client,sid,token)
    try:
        dc.send(json.dumps({'op':'get','path':'source.bin'}))
        meta=await next_json(q);assert meta['size']==size
        dc.send(json.dumps({'op':'pull'}));received=bytearray();index=0
        while True:
            frame=await asyncio.wait_for(q.get(),10)
            if isinstance(frame,str):
                assert json.loads(frame)['op']=='done';break
            received.extend(unpack(frame,index));index+=1
            if index%16==0:dc.send(json.dumps({'op':'pull'}))
        assert bytes(received)==payload
        for _ in range(2):
            dc.send(json.dumps({'op':'put','name':'same.bin','size':size}))
            assert (await next_json(q))['op']=='ready'
            for index,start in enumerate(range(0,size,CHUNK)):
                dc.send(pack(index,payload[start:start+CHUNK]))
                assert (await next_json(q))['op']=='ack'
            dc.send(json.dumps({'op':'finish'}))
            result=await next_json(q);assert result['op']=='saved'
            saved=drop.roots['received']/sid/result['path']
            assert saved.read_bytes()==payload
        assert len(list((drop.roots['received']/sid).glob('*/same.bin')))==2
    finally:
        await asyncio.wait_for(pc.close(),5)


async def test_corruption_never_publishes(client):
    import json
    drop=client.app['drop'];sid,token=drop.store.create('files','shared','',True,24)
    pc,dc,q=await connect(client,sid,token)
    try:
        dc.send(json.dumps({'op':'put','name':'bad.bin','size':10}));await next_json(q)
        dc.send(pack(0,b'1234567890')[:-1]+b'x')
        assert (await next_json(q))['op']=='error'
        assert not list(drop.roots['received'].rglob('bad.bin'))
        assert not list(drop.roots['received'].rglob('.partial*'))
    finally:await pc.close()


async def test_readonly_link_rejects_upload_and_expiry(client):
    import json
    drop=client.app['drop'];sid,token=drop.store.create('files','shared','',False,24)
    pc,dc,q=await connect(client,sid,token)
    try:
        dc.send(json.dumps({'op':'put','name':'bad.bin','size':0}))
        assert (await next_json(q))['op']=='error'
    finally:await pc.close()
    drop.store.db.execute('UPDATE shares SET expires=? WHERE id=?',(time.time()-1,sid));drop.store.db.commit()
    assert (await client.post(f'/api/guest/local/{sid}/browse',json={},headers={'Authorization':'Bearer '+token})).status==403


async def test_remote_server_to_server(client,aiohttp_client,tmp_path):
    sender=await aiohttp_client(create_app(tmp_path/'sdata',tmp_path/'sshared',tmp_path/'sreceived'))
    source=os.urandom(1024*1024+11)
    (sender.app['drop'].roots['shared']/'large.bin').write_bytes(source)
    sid,token=sender.app['drop'].store.create('remote','shared','',False,24)
    link=str(sender.make_url(f'/s/local/{sid}'))+'#'+token
    headers=await login(client)
    response=await client.post('/api/remote/browse',headers=headers,json={'url':link})
    assert response.status==200
    assert (await response.json())['files'][0]['name']=='large.bin'
    response=await client.post('/api/remote/download',headers=headers,json={'url':link,'path':'large.bin'})
    jid=(await response.json())['id']
    for _ in range(150):
        result=await (await client.get('/api/jobs/'+jid)).json()
        if result['state']!='running':break
        await asyncio.sleep(.1)
    assert result['state']=='complete',result
    assert (client.app['drop'].roots['received']/result['path']).read_bytes()==source


async def test_live_revocation_closes_peer(client):
    drop=client.app['drop'];sid,token=drop.store.create('files','shared','',False,24)
    pc,dc,q=await connect(client,sid,token)
    headers=await login(client)
    try:
        assert (await client.delete('/api/shares/'+sid,headers=headers)).status==200
        for _ in range(30):
            if dc.readyState=='closed':break
            await asyncio.sleep(.1)
        assert dc.readyState=='closed'
    finally:await pc.close()


async def test_relay_cannot_register_without_key(client):
    from aiohttp import WSServerHandshakeError
    with pytest.raises(WSServerHandshakeError):await client.ws_connect('/api/relay/not-authorized')


async def test_sso_and_current_membership(aiohttp_client,tmp_path,monkeypatch):
    user={'id':7,'role':'admin'}
    async def hub(request):
        assert request.headers['X-Hub-Key']=='hub-test-key'
        assert request.query['app_id']=='fjorddrop'
        if request.path.endswith('sso-verify'):return web.json_response(dict(ok=True,**user))
        return web.json_response({'ok':True,'items':[user]})
    fake=web.Application();fake.router.add_get('/api/hub/sso-verify',hub);fake.router.add_get('/api/hub/apps/users',hub)
    hub_client=await aiohttp_client(fake)
    monkeypatch.setenv('FJORDHUB_URL',str(hub_client.make_url('')).rstrip('/'))
    monkeypatch.setenv('FJORDHUB_API_KEY','hub-test-key')
    app=await aiohttp_client(create_app(tmp_path/'hdata',tmp_path/'hshared',tmp_path/'hreceived'))
    response=await app.get('/hub-login?token=single-use',allow_redirects=False)
    assert response.status==303
    assert (await app.get('/api/status')).status==200
    user['role']='user'
    assert (await app.get('/api/status')).status==403
