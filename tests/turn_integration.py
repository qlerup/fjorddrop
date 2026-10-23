"""Exercise actual Coturn auth, allocation and bidirectional relay bytes in CI."""
import asyncio
import base64
import hashlib
import hmac
import json
import subprocess
import time
import sys
from pathlib import Path

from aioice import Connection, TransportPolicy


async def main():
    # Read only inside the isolated CI container; never print the secret.
    config=json.loads(Path(sys.argv[1]).read_text()) if len(sys.argv)>1 else json.loads(subprocess.check_output(['docker','exec','fjorddrop-turn','cat','/turn-state/config.json']))
    endpoint=(config['host'],config['port']) if len(sys.argv)>1 else ('127.0.0.1',3478)
    username=str(int(time.time())+600)+':integration'
    password=base64.b64encode(hmac.new(config['secret'].encode(),username.encode(),hashlib.sha1).digest()).decode()
    for transport in ('udp','tcp'):
        kwargs=dict(turn_server=endpoint,turn_username=username,turn_password=password,
                    turn_transport=transport,transport_policy=TransportPolicy.RELAY,use_ipv6=False)
        a,b=Connection(ice_controlling=True,**kwargs),Connection(ice_controlling=False,**kwargs)
        try:
            await asyncio.gather(a.gather_candidates(),b.gather_candidates())
            assert a.local_candidates and b.local_candidates
            assert all(c.type=='relay' for c in a.local_candidates+b.local_candidates)
            for local,remote in ((a,b),(b,a)):
                local.remote_username=remote.local_username
                local.remote_password=remote.local_password
                for candidate in remote.local_candidates:await local.add_remote_candidate(candidate)
                await local.add_remote_candidate(None)
            await asyncio.wait_for(asyncio.gather(a.connect(),b.connect()),15)
            await a.send(b'fjorddrop-upload-through-turn')
            assert await asyncio.wait_for(b.recv(),5)==b'fjorddrop-upload-through-turn'
            await b.send(b'fjorddrop-download-through-turn')
            assert await asyncio.wait_for(a.recv(),5)==b'fjorddrop-download-through-turn'
        finally:await asyncio.gather(a.close(),b.close())
    bad=Connection(ice_controlling=True,turn_server=endpoint,turn_username=username,
                   turn_password='invalid',transport_policy=TransportPolicy.RELAY,use_ipv6=False)
    try:
        await bad.gather_candidates()
        assert not bad.local_candidates,'Coturn accepted invalid credentials'
    finally:await bad.close()
    print('PASS: authenticated relay transfer in both directions over UDP and TCP; invalid password rejected')


if sys.platform=='win32':asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(main())
