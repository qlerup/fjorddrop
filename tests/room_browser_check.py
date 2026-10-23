"""Two browser peers, HTTP -> HTTPS rendezvous, no eager file upload."""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

config=json.loads(Path('test-results/browser-config.json').read_text())
out=Path('test-results');root=Path(config['root'])
with sync_playwright() as p:
    browser=p.chromium.launch()
    host_context=browser.new_context(ignore_https_errors=True,accept_downloads=True,viewport={'width':1440,'height':1050})
    guest_context=browser.new_context(ignore_https_errors=True,accept_downloads=True,viewport={'width':1440,'height':1050})
    if '--vpn' in sys.argv:
        for ctx in (host_context,guest_context):
            ctx.add_init_script("const NativeRTC=window.RTCPeerConnection;window.RTCPeerConnection=class extends NativeRTC{constructor(c){super({...c,iceTransportPolicy:'relay'});}};")
    host=host_context.new_page();guest=guest_context.new_page();errors=[];posts=[]
    for page in (host,guest):
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:posts.append(len(r.post_data_buffer or b'')) if r.method=='POST' else None)
    host.goto(config['local'])
    assert host.locator('header a[href="/library"]').count()==0
    host.screenshot(path=str(out/'room-start-desktop.png'),full_page=True)
    host.locator('#room-create').click()
    host.locator('#room-login input[name=password]').fill('browser-test-password-123')
    host.locator('#room-login button').click()
    host.locator('#room-code').wait_for()
    code=host.locator('#room-code').input_value()
    assert code.startswith('FD1.')
    guest.goto(config['public']+'/connect#'+code)
    guest.locator('#room-files').wait_for(timeout=60000)
    host.locator('#room-files').wait_for(timeout=60000)
    for page in (host,guest):
        assert page.locator('.room-hero').is_hidden()
        assert page.locator('#room-vpn').is_hidden()
    if '--vpn' in sys.argv:
        for page in (host,guest):
            assert page.evaluate("async()=>{const stats=await roomPeer.pc.getStats();let relay=false;stats.forEach(s=>{if(s.type==='candidate-pair'&&s.state==='succeeded'&&s.nominated)relay=stats.get(s.localCandidateId)?.candidateType==='relay'&&stats.get(s.remoteCandidateId)?.candidateType==='relay';});return relay;}")
    for page in (host,guest):page.evaluate('window.showSaveFilePicker=undefined')
    # Real drag/drop: bytes are browser-owned File objects, never posted to app.
    host.evaluate('''() => {
      const dt=new DataTransfer();dt.items.add(new File([new Uint8Array(2100000).map((_,i)=>i%251)],'From-PC.bin'));
      dt.items.add(new File(['small text file'],'Readme.txt'));
      document.getElementById('room-drop').dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt}));
    }''')
    guest.locator('#room-remote-files').get_by_text('From-PC.bin',exact=True).wait_for()
    assert host.evaluate('roomPeer.bytesSent')==0
    assert guest.evaluate('roomPeer.receiving') is None
    assert not list(root.glob('**/From-PC.bin'))
    third=guest_context.new_page();third.goto(config['public']+'/connect#'+code)
    third.locator('#room-notice').wait_for()
    assert third.locator('#room-files').is_hidden()
    with guest.expect_download(timeout=60000) as download:
        guest.locator('.room-file').filter(has_text='From-PC.bin').get_by_role('button').click()
    download.value.save_as(out/'room-downloaded.bin')
    assert (out/'room-downloaded.bin').read_bytes()==bytes(i%251 for i in range(2100000))
    assert host.evaluate('roomPeer.bytesSent')==2100000
    guest.locator('#room-file-input').set_input_files({'name':'Return.txt','mimeType':'text/plain','buffer':b'Only downloaded on request.'})
    host.locator('#room-remote-files').get_by_text('Return.txt',exact=True).wait_for()
    assert guest.evaluate('roomPeer.bytesSent')==0
    with host.expect_download(timeout=30000) as download:
        host.locator('.room-file').filter(has_text='Return.txt').get_by_role('button').click()
    download.value.save_as(out/'room-return.txt')
    assert (out/'room-return.txt').read_bytes()==b'Only downloaded on request.'
    assert not list(root.glob('**/From-PC.bin')) and not list(root.glob('**/Return.txt'))
    assert max(posts)<100000,'File bytes crossed HTTP signaling'
    host.screenshot(path=str(out/'room-connected-desktop.png'),full_page=True)
    for width in (390,320):
        guest.set_viewport_size({'width':width,'height':844})
        assert guest.evaluate('document.documentElement.scrollWidth<=innerWidth')
        guest.screenshot(path=str(out/f'room-connected-{width}.png'),full_page=True)
    guest.locator('#room-leave').click()
    host.locator('#room-start').wait_for(timeout=10000)
    assert not errors,errors
    browser.close()
print('PASS: two peers; HTTP/HTTPS signaling; third peer denied; drag/drop sends zero file bytes; explicit downloads verified both ways; no server copy; responsive UI')
