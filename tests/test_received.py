from types import SimpleNamespace

import pytest
from app import create_app
from transfer import pack


async def login(client):
    await client.post('/api/login',json={'password':'receive-test-password-123'})
    return {'X-CSRF-Token':(await (await client.get('/api/status')).json())['csrf']}


@pytest.mark.asyncio
async def test_http_staging_integrity_download_and_disconnect_cleanup(aiohttp_client,tmp_path,monkeypatch):
    monkeypatch.setenv('ADMIN_PASSWORD','receive-test-password-123')
    app=create_app(tmp_path/'data',tmp_path/'shared',tmp_path/'received');client=await aiohttp_client(app)
    headers=await login(client)
    room=await (await client.post('/api/rooms',json={},headers=headers)).json()
    payload=b'one verified file'*2200
    response=await client.post('/api/received',json={'name':'Test.bin','size':len(payload)},headers=headers)
    assert response.status==200
    ident=(await response.json())['id']
    binary=dict(headers,**{'Content-Type':'application/octet-stream'})
    frames=b''.join(pack(i,payload[n:n+16384]) for i,n in enumerate(range(0,len(payload),16384)))
    assert (await client.post(f'/api/received/{ident}/chunks',data=frames,headers=binary)).status==200
    reply=await client.post(f'/api/received/{ident}/complete',json={},headers=headers)
    assert reply.status==200
    url=(await reply.json())['url']
    response=await client.get(url)
    assert await response.read()==payload and 'attachment' in response.headers['Content-Disposition']
    response=await client.get(url,headers={'Range':'bytes=20-35'})
    assert response.status==206 and await response.read()==payload[20:36]
    # Another incomplete transfer is also removed on disconnect.
    await client.post('/api/received',json={'name':'Unfinished.bin','size':10},headers=headers)
    assert (await client.post(f"/api/guest/local/{room['id']}/room_close",json={'reason':'left'},headers={'Authorization':'Bearer '+room['token']})).status==200
    assert not list(app['drop'].received.root.iterdir())
    assert (await client.get(url)).status==404


@pytest.mark.asyncio
async def test_large_file_is_incremental_and_invalid_chunks_are_removed(aiohttp_client,tmp_path,monkeypatch):
    monkeypatch.setenv('ADMIN_PASSWORD','receive-test-password-123')
    monkeypatch.setattr('received.shutil.disk_usage',lambda _:SimpleNamespace(free=100*1024**3))
    app=create_app(tmp_path/'data',tmp_path/'shared',tmp_path/'received');client=await aiohttp_client(app);headers=await login(client)
    await client.post('/api/rooms',json={},headers=headers)
    assert (await client.post('/api/received',json={'name':'x.bin','size':1})).status==403
    assert (await client.post('/api/received',json={'name':'../x','size':1},headers=headers)).status==400
    response=await client.post('/api/received',json={'name':'22GB.bin','size':22*1024**3},headers=headers)
    assert response.status==200
    ident=(await response.json())['id'];binary=dict(headers,**{'Content-Type':'application/octet-stream'})
    assert (await client.post(f'/api/received/{ident}/complete',json={},headers=headers)).status==400
    assert (await client.post(f'/api/received/{ident}/chunks',data=pack(0,b'x'*16384),headers=binary)).status==200
    assert (app['drop'].received.root/ident/'.partial').stat().st_size<=16384
    # Replayed chunk index must not corrupt or complete a file.
    assert (await client.post(f'/api/received/{ident}/chunks',data=pack(0,b'x'*16384),headers=binary)).status==400
    assert not list(app['drop'].received.root.iterdir())


@pytest.mark.asyncio
async def test_join_stays_on_own_server_and_proxies_signaling(aiohttp_client,tmp_path,monkeypatch):
    monkeypatch.setenv('ADMIN_PASSWORD','receive-test-password-123')
    host_app=create_app(tmp_path/'hdata',tmp_path/'hshared',tmp_path/'hreceived')
    guest_app=create_app(tmp_path/'gdata',tmp_path/'gshared',tmp_path/'greceived')
    host=await aiohttp_client(host_app);guest=await aiohttp_client(guest_app)
    hh=await login(host);gh=await login(guest)
    room=await (await host.post('/api/rooms',json={},headers=hh)).json()
    response=await guest.post('/api/rooms/join',json={'code':room['code']},headers=gh)
    assert response.status==200
    joined=await response.json()
    assert joined['id']!=room['id'] and joined['node']=='local'
    assert guest_app['drop'].rooms.active
    assert (await guest.post('/api/rooms',json={},headers=gh)).status==400
    host_path=f"/api/guest/local/{room['id']}/room_poll"
    guest_path=f"/api/guest/local/{joined['id']}/room_poll"
    signal={'type':'offer','sdp':'v=0\r\n'}
    assert (await host.post(host_path,json={'signal':signal},headers={'Authorization':'Bearer '+room['token']})).status==200
    response=await guest.post(guest_path,json={},headers={'Authorization':'Bearer '+joined['token']})
    assert (await response.json())['messages'][0]['signal']==signal
    ident=(await (await guest.post('/api/received',json={'name':'partial.bin','size':1},headers=gh)).json())['id']
    assert not (tmp_path/'hreceived'/'.fjorddrop-room-cache'/ident).exists()
    await guest.post(guest_path.replace('room_poll','room_close'),json={'reason':'left'},headers={'Authorization':'Bearer '+joined['token']})
    assert not guest_app['drop'].rooms.active and not host_app['drop'].rooms.active
    assert not list(guest_app['drop'].received.root.iterdir())


@pytest.mark.asyncio
async def test_restart_removes_only_private_temporary_files(aiohttp_client,tmp_path,monkeypatch):
    monkeypatch.setenv('ADMIN_PASSWORD','receive-test-password-123')
    root=tmp_path/'received';cache=root/'.fjorddrop-room-cache';folder=cache/('a'*32)
    folder.mkdir(parents=True);(folder/'leftover.bin').write_bytes(b'temporary')
    (root/'keep.bin').write_bytes(b'existing received file')
    app=create_app(tmp_path/'data',tmp_path/'shared',root)
    await aiohttp_client(app)
    assert not list(cache.iterdir()) and (root/'keep.bin').read_bytes()==b'existing received file'
