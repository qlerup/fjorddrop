"""Local HTTPS rendezvous + local HTTP file peer, for end-to-end browser checks."""
import asyncio
import base64
import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp import web, ClientSession, TCPConnector
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from app import create_app


async def main():
    with tempfile.TemporaryDirectory(prefix='fjorddrop-browser-') as tmp:
        root=Path(tmp)
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
        cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(days=1)).not_valid_after(datetime.now(timezone.utc)+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256())
        (root/'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (root/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(root/'cert.pem',root/'key.pem')
        os.environ['ADMIN_PASSWORD']='browser-test-password-123'
        public=create_app(root/'public-data',root/'public-shared',root/'public-received')
        local=create_app(root/'local-data',root/'local-shared',root/'local-received')
        local['drop'].roots['shared'].joinpath('Photos').mkdir()
        local['drop'].roots['shared'].joinpath('Photos','Summer.bin').write_bytes(bytes(range(256))*8193)
        local['drop'].roots['shared'].joinpath('Readme.txt').write_text('Hello from the local HTTP peer!')
        public['drop'].roots['shared'].joinpath('Readme.txt').write_text('A separate HTTPS sender')
        remote_sid,remote_token=public['drop'].store.create('Remote HTTPS files','shared','',False,24)
        if os.getenv('VPN_TEST_CONFIG'):
            config=json.loads(Path(os.environ['VPN_TEST_CONFIG']).read_text())
            username=str(int(time.time())+21600)+':browser-test'
            password=base64.b64encode(hmac.new(config['secret'].encode(),username.encode(),hashlib.sha1).digest()).decode()
            for app in (local,public):
                for k,v in {'turn_url':f"turn:{config['host']}:{config['port']}?transport=tcp",'turn_username':username,'turn_password':password}.items():app['drop'].store.set(k,v)
        rid,secret=public['drop'].store.relay_key()
        for k,v in {'relay_url':'https://localhost:18103','relay_id':rid,'relay_key':secret}.items():local['drop'].store.set(k,v)
        sid,token=local['drop'].store.create('Sommerhuset 2026','shared','',True,24)
        pubrunner=web.AppRunner(public);localrunner=web.AppRunner(local)
        await pubrunner.setup();await localrunner.setup()
        await local['drop'].http.close()
        trust=ssl.create_default_context(cafile=str(root/'cert.pem'))
        local['drop'].http=ClientSession(connector=TCPConnector(ssl=trust))
        await web.TCPSite(pubrunner,'127.0.0.1',18103,ssl_context=tls).start()
        await web.TCPSite(localrunner,'127.0.0.1',18102).start()
        for _ in range(100):
            if local['drop'].relay_status=='Online':break
            await asyncio.sleep(.1)
        Path('test-results').mkdir(exist_ok=True)
        Path('test-results/browser-config.json').write_text(json.dumps({'local':'http://127.0.0.1:18102','public':'https://localhost:18103','link':f'https://localhost:18103/s/{rid}/{sid}#{token}','remote_link':f'https://localhost:18103/s/local/{remote_sid}#{remote_token}','root':str(root),'relay':local['drop'].relay_status}))
        print('READY',local['drop'].relay_status,flush=True)
        try:await asyncio.Event().wait()
        finally:await localrunner.cleanup();await pubrunner.cleanup()

if sys.platform=='win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(main())
