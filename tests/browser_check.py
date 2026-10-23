import hashlib
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright

config=json.loads(Path('test-results/browser-config.json').read_text())
assert config['relay']=='Online',config
out=Path('test-results');root=Path(config['root'])
with sync_playwright() as p:
    browser=p.chromium.launch()
    context=browser.new_context(ignore_https_errors=True,accept_downloads=True,viewport={'width':1440,'height':1100})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(config['local'])
    page.locator('#login-form input[name=password]').fill('browser-test-password-123')
    page.locator('#login-form button').click()
    page.locator('#files').get_by_text('Photos',exact=True).wait_for()
    page.screenshot(path=str(out/'admin-desktop.png'),full_page=True)
    page.locator('#new-share').click()
    page.locator('#share-form input[name=name]').fill('Test invitation')
    page.locator('#share-form button.primary').click()
    page.locator('#share-link').wait_for()
    assert page.locator('#share-link').input_value().startswith(config['public']+'/s/')
    page.locator('[data-close=link-dialog]').click()
    page.locator('#settings-open').click()
    page.screenshot(path=str(out/'settings-desktop.png'),full_page=True)
    page.locator('[data-close=settings-dialog]').click()
    guest=context.new_page();guest.on('pageerror',lambda error:errors.append(str(error)))
    guest.goto(config['link'])
    guest.locator('#files').get_by_text('Photos',exact=True).wait_for()
    # Prove page reload preserves the capability within this tab.
    guest.reload();guest.locator('#files').get_by_text('Photos',exact=True).wait_for()
    guest.screenshot(path=str(out/'guest-desktop.png'),full_page=True)
    guest.evaluate('window.showSaveFilePicker=undefined')
    with guest.expect_download(timeout=60000) as event:
        guest.locator('.file-row').filter(has_text='Readme.txt').get_by_role('button').click()
    download=event.value;download.save_as(out/'received-readme.txt')
    assert (out/'received-readme.txt').read_bytes()==(root/'local-shared/Readme.txt').read_bytes()
    guest.locator('.file-row').filter(has_text='Photos').get_by_role('button').click()
    with guest.expect_download(timeout=60000) as event:
        guest.locator('.file-row').filter(has_text='Summer.bin').get_by_role('button').click()
    event.value.save_as(out/'received-large.bin')
    assert (out/'received-large.bin').read_bytes()==(root/'local-shared/Photos/Summer.bin').read_bytes()
    guest.locator('#upload').set_input_files({'name':'Return.bin','mimeType':'application/octet-stream','buffer':bytes(range(256))*2049})
    guest.locator('#transfer-description').filter(has_text='Gemt i destinationsmappen').wait_for(timeout=60000)
    targets=list((root/'local-received').rglob('Return.bin'))
    assert len(targets)==1 and targets[0].read_bytes()==bytes(range(256))*2049
    for width in (390,320):
        guest.set_viewport_size({'width':width,'height':844})
        guest.screenshot(path=str(out/f'guest-{width}.png'),full_page=True)
        assert guest.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
    # Admin HTTP -> remote HTTPS catalog -> direct server receipt.
    page.locator('#remote-open').click()
    page.locator('#remote-form input[name=url]').fill(config['link'])
    page.locator('#remote-form button.primary').click()
    page.locator('#library-title').filter(has_text='ekstern deling').wait_for()
    page.locator('.file-row').filter(has_text='Readme.txt').get_by_text('Gem på server',exact=True).click()
    page.locator('#transfer-description').filter(has_text='Gemt på serveren').wait_for(timeout=60000)
    assert list((root/'local-received').glob('download-*/Readme.txt'))
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path=str(out/'remote-mobile.png'),full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors,errors
    browser.close()
print('PASS: HTTPS rendezvous -> HTTP sender; real binary downloads/upload; server download; refresh; desktop/mobile; no JS errors')
