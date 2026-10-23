"""Opt-in live gateway check against an isolated test deployment, never CI."""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

base,password_file,profile=sys.argv[1:]
with sync_playwright() as p:
    browser=p.chromium.launch()
    c=browser.new_context(accept_downloads=True)
    login=c.request.post(base+'/api/login',data={'password':Path(password_file).read_text()})
    assert login.ok
    host=c.new_page();guest=browser.new_page(accept_downloads=True)
    errors=[]
    for page in (host,guest):page.on('pageerror',lambda e:errors.append(str(e)))
    host.goto(base)
    host.locator('#room-vpn').wait_for()
    host.locator('#room-vpn summary').click()
    host.locator('#room-vpn-profile').select_option(profile)
    host.locator('#room-vpn-save').click()
    host.locator('#room-vpn-status').filter(has_text='gennem FjordVPN').wait_for(timeout=30000)
    host.locator('#room-create').click();host.locator('#room-code').wait_for()
    code=host.locator('#room-code').input_value()
    guest.goto(base+'/connect#'+code)
    host.wait_for_timeout(10000)
    for page in (host,guest):
        print('Browser connection diagnostic:',page.evaluate("({notice:document.getElementById('room-notice').textContent,state:roomPeer?.pc.connectionState,gathering:roomPeer?.pc.iceGatheringState,signal:roomPeer?.pc.signalingState,relayCandidates:(roomPeer?.pc.localDescription?.sdp.match(/typ relay/g)||[]).length})"),flush=True)
    print('Browser errors:',errors,flush=True)
    for page in (host,guest):
        page.locator('#room-files').wait_for(timeout=60000)
        assert page.evaluate('roomPeer.pc.getConfiguration().iceTransportPolicy')=='relay'
        assert 'FjordVPN' in page.locator('#room-state').inner_text()
        page.evaluate('window.showSaveFilePicker=undefined')
    payload=bytes(range(256))*4097
    host.locator('#room-file-input').set_input_files({'name':'VPN-test.bin','mimeType':'application/octet-stream','buffer':payload})
    guest.locator('#room-remote-files').get_by_text('VPN-test.bin',exact=True).wait_for()
    assert host.evaluate('roomPeer.bytesSent')==0
    with guest.expect_download(timeout=60000) as event:
        guest.locator('#room-remote-files button').click()
    path=Path('test-results/vpn-browser-received.bin');event.value.save_as(path);assert path.read_bytes()==payload
    for page in (host,guest):
        pairs=page.evaluate("async()=>{const s=await roomPeer.pc.getStats();return [...s.values()].filter(p=>p.type==='candidate-pair').map(p=>({state:p.state,nominated:p.nominated,local:s.get(p.localCandidateId)?.candidateType,remote:s.get(p.remoteCandidateId)?.candidateType}));}")
        print('ICE candidate types:',pairs,flush=True)
        assert any(p.get('state')=='succeeded' and p.get('nominated') and p.get('local')=='relay' for p in pairs)
    host.screenshot(path='test-results/vpn-selected-browser.png',full_page=True)
    # Disabling VPN closes the room before removing its owned relay.
    host.locator('#room-vpn-profile').select_option('')
    host.locator('#room-vpn-save').click()
    guest.locator('#room-start').wait_for(timeout=15000)
    host.locator('#room-vpn-status').filter(has_text='ikke valgt').wait_for(timeout=15000)
    assert not errors,errors
    browser.close()
print('PASS: select live FjordVPN in UI; automatic relay; relay-only browser transfer; exact bytes; no eager upload; disconnect and disable')
