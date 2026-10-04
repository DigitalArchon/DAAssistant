"""Draw the application icon, at every size the tray and the launcher ask for.

The output is committed under assistant/data/icons, so the running app and the AppImage
use the same file rather than two drawings that drift apart. Re-run this after editing
the shapes below:

    python3 packaging/make-icon.py

The icon has to read at 22px in a panel as well as at 256px in a launcher, which is why
there is a size set rather than one master: below 32px the three dots turn to mush and
the selection brackets need proportionally heavier strokes to survive at all. Each size
is drawn at 4x and downsampled, which is cheaper than antialiasing every edge by hand.
"""

import os
import sys

from PIL import Image, ImageDraw

SIZES = (16, 22, 24, 32, 48, 64, 128, 256)
SCALE = 4
BACKDROP = (37, 44, 71)       # deep slate, so the white shapes carry the contrast
ACCENT = (129, 140, 248)      # indigo, for the selection frame
INK = (255, 255, 255)


def draw(size):
    s = size * SCALE
    small = size < 32
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)

    pen.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=BACKDROP)

    # Selection frame: four corner brackets rather than a full rectangle, which stays
    # legible when the whole icon is 22 pixels wide. As the icon shrinks the strokes get
    # proportionally heavier, or they vanish in the downsample, and the brackets move
    # outwards to keep clear of the bubble - at 16px a one-pixel gap is the difference
    # between a frame and a smudge.
    m = int(s * (0.16 if small else 0.20))
    arm = int(s * (0.15 if small else 0.13))
    w = max(2, int(s * (0.055 if small else 0.035)))
    for x, y, dx, dy in ((m, m, 1, 1), (s - m, m, -1, 1),
                         (m, s - m, 1, -1), (s - m, s - m, -1, -1)):
        pen.line((x, y, x + arm * dx, y), fill=ACCENT, width=w)
        pen.line((x, y, x, y + arm * dy), fill=ACCENT, width=w)

    # Speech bubble, sitting inside the frame.
    b = int(s * (0.34 if small else 0.31))
    overhang = int(s * (0.03 if small else 0.07))
    pen.rounded_rectangle((b, int(s * 0.34), s - b + overhang, int(s * 0.60)),
                          radius=int(s * 0.055), fill=INK)
    pen.polygon(((int(s * 0.40), int(s * 0.58)), (int(s * 0.40), int(s * 0.72)),
                 (int(s * 0.53), int(s * 0.58))), fill=INK)

    # Three dots: the universal "it is thinking" mark. At panel sizes they close up into
    # a grey smear, so the bubble is left clean instead.
    if not small:
        r, cy = max(1, int(s * 0.022)), int(s * 0.47)
        for cx in (int(s * 0.40), int(s * 0.50), int(s * 0.60)):
            pen.ellipse((cx - r, cy - r, cx + r, cy + r), fill=BACKDROP)

    return image.resize((size, size), Image.LANCZOS)


def write_theme(root):
    """Write a hicolor icon theme tree, the layout every icon lookup expects."""
    written = []
    for size in SIZES:
        directory = os.path.join(root, "hicolor", f"{size}x{size}", "apps")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "da-assistant.png")
        draw(size).save(path)
        written.append(path)
    return written


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "assistant", "data", "icons")
    for path in write_theme(out):
        print(f"wrote {os.path.relpath(path, here)}")
