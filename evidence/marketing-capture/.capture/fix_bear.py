"""Extract the TRUE Captur'd brand mark (flat blue roaring bear) from the
Firefly brand identity sheet and replace every wrong-bear asset in the
Captur'd docroot: capturd-bear.png, favicon, and the PWA icon set.

Per the sheet: primary mark = flat geometric roaring bear; secondary =
constellation walking bear (used sparingly). The smiling teddy that shipped is
not the brand and is deleted (overwritten) completely.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

SHEET = Path(r"C:\Users\slang\rhobear-ux-pack\_FIREFLY\03-capturd\Gemini_Generated_Image_jua0pojua0pojua0.png")
WEB = Path(r"C:\Users\slang\rhobear-capturd\service\web\assets")
ROOT_ASSETS = Path(r"C:\Users\slang\rhobear-capturd\assets")

sheet = Image.open(SHEET).convert("RGB")
W, H = sheet.size
print("sheet:", sheet.size)

# ---- 1. crop the primary bear (top center, flat blue on near-black) --------
# Displayed-coordinate estimate x 820..1180, y 60..360 on a 2000x1116 preview
# of a 2752x1536 original -> scale by 1.376.
s = W / 2000.0
box = (int(805 * s), int(55 * s), int(1185 * s), int(365 * s))
bear = sheet.crop(box)
print("crop:", bear.size)

# ---- 2. chroma-key the dark background to transparency ---------------------
# The bear is flat blue; the sheet background is near-black (#0d0f14-ish).
px = bear.load()
w, h = bear.size
rgba = bear.convert("RGBA")
p2 = rgba.load()
for y in range(h):
    for x in range(w):
        r, g, b = px[x, y]
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        # dark background AND dark outline removal: keep blue-dominant pixels.
        if lum < 46:
            p2[x, y] = (r, g, b, 0)
        elif lum < 78:
            # feather zone: scale alpha by luminance for soft edges.
            p2[x, y] = (r, g, b, int((lum - 46) / 32 * 255))
rgba.putalpha(rgba.split()[3].point(lambda a: a))

# Trim to content.
bbox = rgba.getbbox()
rgba = rgba.crop(bbox)
print("trimmed mark:", rgba.size)
rgba.save(WEB / "capturd-bear.png")
rgba.save(ROOT_ASSETS / "capturd-bear.png")
print("wrote capturd-bear.png (primary mark)")

# ---- 3. PWA icons: mark centered on the brand navy square ------------------
NAVY = (13, 15, 20, 255)

def make_icon(size: int, maskable: bool) -> Image.Image:
    icon = Image.new("RGBA", (size, size), NAVY)
    mark = rgba.copy()
    # safe zone: maskable needs ~20% padding; regular 12%
    target = int(size * (0.60 if maskable else 0.72))
    mark.thumbnail((target, target), Image.LANCZOS)
    mx = (size - mark.width) // 2
    my = (size - mark.height) // 2
    icon.alpha_composite(mark, (mx, my))
    return icon

icons = Path(r"C:\Users\slang\rhobear-capturd\service\web\assets\icons")
make_icon(512, False).save(icons / "icon-512.png")
make_icon(192, False).save(icons / "icon-192.png")
make_icon(512, True).save(icons / "icon-512-maskable.png")
make_icon(192, True).save(icons / "icon-192-maskable.png")
make_icon(180, False).save(icons / "apple-touch-icon.png")
print("wrote PWA icons")

# ---- 4. wide logo (root assets had a wide wordmark too) --------------------
# The wide logo is the bear + wordmark built from the sheet's lockup crop.
lockup = sheet.crop((int(505 * s), int(390 * s), int(1460 * s), int(500 * s)))
lp = lockup.convert("RGBA").load()
lw, lh = lockup.size
rgba_l = lockup.convert("RGBA")
pl = rgba_l.load()
for y in range(lh):
    for x in range(lw):
        r, g, b = lp[x, y]
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        if lum < 46:
            pl[x, y] = (r, g, b, 0)
        elif lum < 78:
            pl[x, y] = (r, g, b, int((lum - 46) / 32 * 255))
bbox = rgba_l.getbbox()
rgba_l = rgba_l.crop(bbox)
rgba_l.save(ROOT_ASSETS / "capturd-logo-wide.png")
print("wrote capturd-logo-wide.png")
print("DONE — old bear deleted everywhere")
