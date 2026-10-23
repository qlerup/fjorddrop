# FjordDrop brand kit

Editable original: `icon-master.svg` (also `static/icon.svg`). The separated cubes represent file pieces, the curved arrow their transfer. Lavender + mint on the dark FjordHub palette, with the rounded app tile used by the other Fjord apps. This artwork is original SVG, not a raster wrapper.

Exports: transparent PNG master, icons 16–1024 px, Apple touch icon, multi-resolution ICO, maskable Android/PWA icon, dark/light SVG + PNG wordmarks, 1200×630 social card, and `fjorddrop-brand-kit.zip`. `exports.json` lists all outputs.

Rebuild: `python tools/build_brand.py` with Pillow and Playwright Chromium installed. Palette: background `#10151b`, lavender `#b5adff`, mint `#7bdbbf`, text `#f0f3f5`. The UI uses local system fonts without external font requests. Preserve icon aspect ratio and keep adequate clear space around the wordmark.
