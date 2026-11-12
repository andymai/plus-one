# /// script
# requires-python = ">=3.11"
# dependencies = ["fonttools", "brotli"]
# ///
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

STATIC = Path(__file__).resolve().parent.parent / "plus1" / "web" / "static"
TILE = "#5e6ad2"
WHITE = "#ffffff"
AMBER = "#ffc53d"
WEIGHT = 900
CAP = 1490
STEM = 404
PLUS_SIZE = 0.66 * CAP
GAP = 0.32 * STEM
INK = 0.52
MAX_WIDTH = 0.8


def digits():
    font = instantiateVariableFont(TTFont(STATIC / "inter.woff2"), {"wght": WEIGHT})
    glyph_set = font.getGlyphSet()
    cmap = font.getBestCmap()
    table = {}
    for ch in "123456789":
        glyph = glyph_set[cmap[ord(ch)]]
        rec, bounds = RecordingPen(), BoundsPen(glyph_set)
        glyph.draw(rec)
        glyph.draw(bounds)
        table[ch] = (rec, bounds.bounds)
    return table


def plus():
    c, s, h = PLUS_SIZE / 2, PLUS_SIZE / 2, STEM / 2
    y = CAP / 2
    pts = [
        (c - h, y + s),
        (c + h, y + s),
        (c + h, y + h),
        (c + s, y + h),
        (c + s, y - h),
        (c + h, y - h),
        (c + h, y - s),
        (c - h, y - s),
        (c - h, y - h),
        (c - s, y - h),
        (c - s, y + h),
        (c - h, y + h),
    ]
    rec = RecordingPen()
    rec.moveTo(pts[0])
    for p in pts[1:]:
        rec.lineTo(p)
    rec.closePath()
    return rec


def path(rec, transform):
    pen = SVGPathPen(None, ntos=lambda n: f"{n:.1f}".rstrip("0").rstrip("."))
    rec.replay(TransformPen(pen, transform))
    return pen.getCommands()


def mark(glyphs, digit, size, ink, cx, cy, digit_color=WHITE, plus_color=WHITE):
    rec, (x0, _, x1, _) = glyphs[digit]
    shift = PLUS_SIZE + GAP - x0
    width = shift + x1
    ink = min(ink, MAX_WIDTH * CAP / width)
    k = ink * size / CAP
    ox = cx - width * k / 2
    oy = cy + CAP * k / 2
    return (
        f'<path fill="{plus_color}" d="{path(plus(), (k, 0, 0, -k, ox, oy))}"/>'
        f'<path fill="{digit_color}" d="{path(rec, (k, 0, 0, -k, ox + shift * k, oy))}"/>'
    )


def svg(size, body, defs=""):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">{defs}{body}</svg>\n'
    )


def favicon(glyphs, digit, color):
    tile = f'<rect width="512" height="512" rx="112" fill="{TILE}"/>'
    return svg(512, tile + mark(glyphs, digit, 512, INK, 256, 256, digit_color=color))


def square(glyphs, size, ink):
    return svg(
        size,
        f'<rect width="{size}" height="{size}" fill="{TILE}"/>'
        + mark(glyphs, "1", size, ink, size / 2, size / 2),
    )


def macos(glyphs):
    defs = (
        '<defs><filter id="s" x="-10%" y="-10%" width="120%" height="130%">'
        '<feDropShadow dx="0" dy="10" stdDeviation="10" flood-color="#000" flood-opacity=".28"/></filter></defs>'
    )
    tile = (
        f'<rect x="100" y="100" width="824" height="824" rx="185" fill="{TILE}" filter="url(#s)"/>'
    )
    return svg(1024, tile + mark(glyphs, "1", 824, INK, 512, 512), defs)


def badge(glyphs):
    return svg(96, mark(glyphs, "1", 96, 0.62, 48, 48))


PNGS = {
    "icon-192.png": ("macos", 192),
    "icon-512.png": ("macos", 512),
    "icon-maskable-512.png": ("maskable", 512),
    "apple-touch-icon.png": ("touch", 180),
    "badge-96.png": ("badge", 96),
}


def main():
    if not shutil.which("rsvg-convert"):
        sys.exit("rsvg-convert is required (librsvg).")
    glyphs = digits()
    svgs = {
        "icon.svg": favicon(glyphs, "1", WHITE),
        **{f"fav-{d}.svg": favicon(glyphs, d, AMBER) for d in "123456789"},
    }
    sources = {
        "macos": macos(glyphs),
        "touch": square(glyphs, 180, INK),
        "maskable": square(glyphs, 512, INK * 0.86),
        "badge": badge(glyphs),
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        for key, text in sources.items():
            (out / f"{key}.svg").write_text(text)
        for name, (key, size) in PNGS.items():
            subprocess.run(
                [
                    "rsvg-convert",
                    "-w",
                    str(size),
                    "-h",
                    str(size),
                    "-o",
                    str(out / name),
                    str(out / f"{key}.svg"),
                ],
                check=True,
            )
        for name, text in svgs.items():
            (out / name).write_text(text)
        for name in [*svgs, *PNGS]:
            shutil.copyfile(out / name, STATIC / name)


if __name__ == "__main__":
    main()
