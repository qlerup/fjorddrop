"""Reproducible exports from our editable SVG master; Pillow + Playwright required."""
import io
import json
from pathlib import Path
import zipfile
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'static/icons'
BRAND = ROOT/'branding'
OUT.mkdir(parents=True, exist_ok=True)
BRAND.mkdir(exist_ok=True)
svg = (ROOT/'static/icon.svg').read_text(encoding='utf-8')
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': 1024, 'height': 1024}, device_scale_factor=1)
    page.set_content('<style>body{margin:0}svg{width:1024px;height:1024px}</style>'+svg)
    master = Image.open(io.BytesIO(page.screenshot(omit_background=True))).convert('RGBA')
    master.save(BRAND/'icon-master.png')
    for size in [16, 32, 48, 64, 128, 180, 192, 256, 512, 1024]:
        master.resize((size,size),Image.Resampling.LANCZOS).save(OUT/f'icon-{size}.png')
    master.resize((180,180),Image.Resampling.LANCZOS).save(OUT/'apple-touch-icon.png')
    master.save(OUT/'favicon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
    mask=Image.new('RGBA',(512,512),'#201f32')
    icon=master.resize((400,400),Image.Resampling.LANCZOS)
    mask.alpha_composite(icon,(56,56));mask.save(OUT/'maskable-512.png')
    for variant, text in [('dark','#f0f3f5'),('light','#1a2330')]:
        inner=svg[svg.index('<defs>'):svg.rindex('</svg>')]
        wordmark=f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="240" viewBox="0 0 960 240"><g transform="translate(0 20) scale(.39)">{inner}</g><text x="225" y="144" font-family="Segoe UI,Arial,sans-serif" font-size="100" font-weight="650" letter-spacing="-4" fill="{text}">Fjord<tspan fill="#9181db">Drop</tspan></text></svg>'
        (BRAND/f'logo-{variant}.svg').write_text(wordmark,encoding='utf-8')
        page.set_viewport_size({'width':960,'height':240})
        page.set_content('<style>body{margin:0}</style>'+wordmark)
        page.screenshot(path=str(BRAND/f'logo-{variant}.png'),omit_background=True)
    page.set_viewport_size({'width':1200,'height':630})
    page.set_content('<style>body{margin:0;background:#10151b;color:#f0f3f5;font-family:Segoe UI,Arial;display:flex;align-items:center;padding:100px;gap:70px;height:430px}svg{width:300px;height:300px;flex-shrink:0}h1{font-size:72px;letter-spacing:-3px;margin:0}p{font-size:28px;color:#a5b1be;line-height:1.5}small{color:#b5adff;letter-spacing:4px;font-size:16px}</style>'+svg+'<div><small>FRA DIG TIL DEM</small><h1>FjordDrop</h1><p>Store filer.<br>En lille genvej.</p></div>')
    page.screenshot(path=str(BRAND/'social-1200x630.png'))
    browser.close()
(BRAND/'icon-master.svg').write_text(svg,encoding='utf-8')
assets=sorted([p for p in OUT.iterdir() if p.is_file()]+[p for p in BRAND.iterdir() if p.suffix in ('.svg','.png')])
(BRAND/'exports.json').write_text(json.dumps([str(p.relative_to(ROOT)).replace('\\','/') for p in assets],indent=2)+'\n')
with zipfile.ZipFile(BRAND/'fjorddrop-brand-kit.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for asset in assets:
        archive.write(asset,asset.relative_to(ROOT))
print('Exported',len(assets),'assets and brand-kit ZIP')
