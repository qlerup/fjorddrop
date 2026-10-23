import time
import pytest
from app import create_app
from vpn import force_relay
from aiortc import RTCPeerConnection
from aioice import TransportPolicy
from types import SimpleNamespace
from vpn import bridge_network


@pytest.mark.asyncio
async def test_one_room_two_people_no_bytes_and_expiry(aiohttp_client,tmp_path,monkeypatch):
    monkeypatch.setenv('ADMIN_PASSWORD','rooms-test-password-123')
    app=create_app(tmp_path/'data',tmp_path/'shared',tmp_path/'received')
    client=await aiohttp_client(app)
    await client.post('/api/login',json={'password':'rooms-test-password-123'})
    status=await (await client.get('/api/status')).json()
    headers={'X-CSRF-Token':status['csrf']}
    reply=await client.post('/api/rooms',json={},headers=headers)
    assert reply.status==200
    room=await reply.json()
    assert (await client.post('/api/rooms',json={},headers=headers)).status==400
    drop=app['drop'];r=drop.rooms.room
    join=await drop.rooms.action(room['id'],r['invite'],'room_join',{})
    with pytest.raises(ValueError):await drop.rooms.action(room['id'],r['invite'],'room_join',{})
    with pytest.raises(PermissionError):await drop.rooms.action(room['id'],'bad','room_poll',{})
    offer={'type':'offer','sdp':'v=0\r\n'}
    await drop.rooms.action(room['id'],room['token'],'room_poll',{'signal':offer})
    response=await drop.rooms.action(room['id'],join['token'],'room_poll',{})
    assert response['messages'][0]['signal']==offer
    ack=response['messages'][0]['id']
    assert not (await drop.rooms.action(room['id'],join['token'],'room_poll',{'ack':ack}))['messages']
    with pytest.raises(ValueError):await drop.rooms.action(room['id'],join['token'],'room_poll',{'signal':{'type':'file','sdp':'contents'}})
    assert not list((tmp_path/'shared').iterdir()) and not list((tmp_path/'received').iterdir())
    pc=RTCPeerConnection()
    with pytest.raises(ValueError):drop.claim_connection(pc)
    await pc.close()
    r['people']['guest']['seen']=time.time()-76
    assert not drop.rooms.active
    assert (await client.post('/api/rooms',json={},headers=headers)).status==200


@pytest.mark.asyncio
async def test_vpn_fails_closed_and_adapter_forbids_host_candidates(tmp_path):
    drop=create_app(tmp_path/'data',tmp_path/'shared',tmp_path/'received')['drop']
    drop.vpn.profile='a'*32
    with pytest.raises(ValueError):drop.ice()
    drop.vpn.config={'host':'203.0.113.1','port':50000,'local_host':'172.17.0.3','local_port':60000}
    drop.vpn.updated=time.monotonic()
    assert '203.0.113.1:50000' in drop.ice()[0]['urls']
    assert '172.17.0.3:60000' in drop.ice(local=True)[0]['urls']
    pc=RTCPeerConnection();pc.createDataChannel('fjorddrop-v1')
    force_relay(pc)
    assert pc.sctp.transport.transport.iceGatherer._connection._transport_policy is TransportPolicy.RELAY
    await pc.setLocalDescription(await pc.createOffer())
    assert ' typ host' not in pc.localDescription.sdp
    await pc.close();drop.store.db.close()


def test_modern_docker_network_inspect():
    c=SimpleNamespace(attrs={'NetworkSettings':{'Networks':{'bridge':{'Gateway':'172.17.0.1','IPAddress':'172.17.0.3'}}}})
    assert bridge_network(c)['Gateway']=='172.17.0.1'
    c.attrs['NetworkSettings']['Networks']['bridge']['Gateway']=''
    with pytest.raises(ValueError):bridge_network(c)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,expected', [('vpn','VPN-forbindelsen var ikke klar'), ('connection_failed','Den anden kunne ikke opretholde'), ('left','Den anden har forladt')])
async def test_closed_room_explains_cause_to_participants_only(tmp_path,reason,expected):
    drop=create_app(tmp_path/'data',tmp_path/'shared',tmp_path/'received')['drop']
    drop.rooms.room={'id':'room','expires':time.time()+60,'people':{'host':{'token':'host-token','seen':time.time(),'queue':[]}}}
    await drop.close_transfers(reason)
    # A second cleanup must not replace the original failure with a generic one.
    drop.rooms.close()
    with pytest.raises(ValueError,match=expected):
        await drop.rooms.action('room','host-token','room_poll',{})
    with pytest.raises(ValueError,match='Forbindelsen er afsluttet'):
        await drop.rooms.action('room','unknown','room_poll',{})
    drop.store.db.close()
