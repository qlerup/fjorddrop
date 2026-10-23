"""HTTPS sender -> HTTP receiver's own server -> automatic PC download."""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

config=json.loads(Path('test-results/browser-config.json').read_text())
root=Path(config['root']);out=Path('test-results')
with sync_playwright() as p:
    browser=p.chromium.launch()
    hc=browser.new_context(ignore_https_errors=True,accept_downloads=True)
    gc=browser.new_context(ignore_https_errors=True,accept_downloads=True)
    if '--vpn' in sys.argv:
        for ctx in (hc,gc):ctx.add_init_script("const NativeRTC=window.RTCPeerConnection;window.RTCPeerConnection=class extends NativeRTC{constructor(c){super({...c,iceTransportPolicy:'relay'});}};")
    for ctx,base in ((hc,config['public']),(gc,config['local'])):
        assert ctx.request.post(base+'/api/login',data={'password':'browser-test-password-123'}).ok
    host=hc.new_page();guest=gc.new_page();errors=[];batches=[]
    for page in (host,guest):page.on('pageerror',lambda e:errors.append(str(e)))
    guest.on('request',lambda r:batches.append(len(r.post_data_buffer or b'')) if r.url.endswith('/chunks') else None)
    host.goto(config['public']);host.locator('#room-vpn').wait_for();host.locator('#room-create').click();host.locator('#room-code').wait_for()
    code=host.locator('#room-code').input_value()
    guest.goto(config['local']);guest.locator('#room-vpn').wait_for()
    guest.locator('#room-code-input').fill(code);guest.locator('#room-join button').click()
    try:
        for page in (host,guest):page.locator('#room-files').wait_for(timeout=60000)
    except Exception:
        for label,page in (('sender',host),('receiver',guest)):
            print(label,page.evaluate("({notice:document.getElementById('room-notice').textContent,state:document.getElementById('room-state').textContent,network:roomPeer?.diagnostic()})"),flush=True)
        raise
    assert guest.url.startswith(config['local']), 'Receiver navigated to the sender instead of staying on its own server'
    payload=bytes(range(256))*8193
    host.locator('#room-file-input').set_input_files({'name':'Server-stage.bin','mimeType':'application/octet-stream','buffer':payload})
    guest.locator('#room-remote-files').get_by_text('Server-stage.bin',exact=True).wait_for()
    assert not list(root.glob('**/Server-stage.bin')) and not batches
    with guest.expect_download(timeout=60000) as event:guest.locator('#room-remote-files button').click()
    received=out/'http-server-to-pc.bin';event.value.save_as(received)
    assert received.read_bytes()==payload
    temporary=list((root/'local-received').glob('**/Server-stage.bin'))
    assert len(temporary)==1 and temporary[0].read_bytes()==payload
    assert not list((root/'public-received').glob('**/Server-stage.bin'))
    assert batches and max(batches)<=16*(16384+36)
    guest.screenshot(path=str(out/'http-server-received.png'),full_page=True)
    guest.locator('#room-leave').click();host.locator('#room-start').wait_for(timeout=10000)
    assert not temporary[0].exists()
    assert not errors,errors
    browser.close()
print('PASS: HTTPS sender; own HTTP receiver; bounded server staging; automatic PC download; exact bytes; no eager upload; delete on disconnect')
