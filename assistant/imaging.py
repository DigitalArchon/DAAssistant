"""Normalise captured images before they go to a vision model.

Why this module exists: a raw PIL image sent straight as base64 PNG goes wrong in three
ways.

  1. Small crops stay small. A 300x80 crop of 11px text is genuinely hard for a VLM to
     transcribe. Upscaling with a good resampling filter before encoding measurably helps,
     because the model's image encoder tiles at a fixed patch size.
  2. RGBA is not flattened. grim and some portal backends hand back an alpha channel;
     encoders composite that against black, which darkens text.
  3. Nothing is capped, so a 4K fullscreen grab is downsampled by the provider in a way
     we neither control nor see.

Note upscaling adds no real detail - it only stops the model's own downsampling from
destroying detail that is already there, and gives the patch grid more pixels per glyph.
Past roughly 3x it is pure token cost, so it is capped.

And nothing but pixels leaves. Every image that is sent or shown is rebuilt from its raw
pixel bytes first (pixels_only), so whatever the source carried - an ICC profile, EXIF,
PNG text chunks, a portal's own annotations - has nowhere to ride along. Pillow copies
an image's .info through convert() and resize(), and its PNG writer embeds .info's ICC
profile unasked, so encoding the same Image object would not be enough.
"""

import base64
import datetime
import os
from io import BytesIO

from PIL import Image

from .config import DEFAULTS

MAX_UPSCALE = 3.0


def flatten(img):
    """Composite any transparency onto white and return RGB."""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return img.convert("RGB")


def pixels_only(img):
    """A brand-new image made from `img`'s pixel bytes alone: no .info, so no metadata of
    any kind can be carried over into what is encoded from it."""
    return Image.frombytes(img.mode, img.size, img.tobytes())


def black_out(img, boxes):
    """`img` flattened to RGB with each (x, y, w, h) box painted solid black.

    The black is written into the one bitmap, with whole-pixel edges - no layer to
    remove, and no anti-aliased border left partly showing what was under it. Boxes are
    clipped to the image; an empty list still returns a fresh RGB copy.
    """
    out = flatten(img).copy()
    width, height = out.size
    for x, y, w, h in boxes:
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(width, int(x + w)), min(height, int(y + h))
        if x1 > x0 and y1 > y0:
            out.paste((0, 0, 0), (x0, y0, x1, y1))
    return out


def rescale(img, target_long_edge=1600, max_long_edge=2000, upscale=True):
    """Bring the long edge into a range a vision encoder handles well."""
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    long_edge = max(w, h)

    if long_edge > max_long_edge:
        factor = max_long_edge / long_edge          # too big: downsample
    elif upscale and long_edge < target_long_edge:
        factor = min(target_long_edge / long_edge, MAX_UPSCALE)   # too small: upscale
    else:
        return img

    new_size = (max(1, round(w * factor)), max(1, round(h * factor)))
    if new_size == (w, h):
        return img
    # LANCZOS both up and down: sharpest of PIL's filters for text edges.
    return img.resize(new_size, Image.LANCZOS)


# How hard zlib works on a PNG. Every picture is encoded on the main loop - a capture as
# its review dialog opens, an attachment as it is added - so this is time spent looking
# at nothing. Pillow's optimize=True, which this used to ask for, is level 9 and a search
# for the best strategy besides: measured on screenshots of text, 290 ms for a 1600x900
# region and 430 ms for the largest picture sent, to come out 3.5% smaller than this
# level does in 50 and 75 ms. A lower level saves little more time and costs more bytes.
# Lossless at any level: the pixels sent are the same.
PNG_COMPRESS_LEVEL = 6


def to_png_bytes(img):
    buf = BytesIO()
    img.save(buf, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
    return buf.getvalue()


def screenshot_png(img):
    """The capture as the user took it, for them to view and save: no flattening or
    scaling, which are for the model. Still pixels only: it is shown, and saved wherever
    the user saves it."""
    return to_png_bytes(pixels_only(img))


def prepare_for_vision(img, config=None, save_debug_copy=False):
    """Full pipeline. Returns (base64_png, info_dict).

    info_dict carries the real dimensions and payload size so the confirm dialog can show
    them - a bad capture should be visible before it is sent, not after the model fails.
    """
    # Defaults come from config, not from a second copy of the numbers here: the cap
    # decides whether a long identifier survives the downscale, and two sources of truth
    # for that is how it would quietly go back to the old value.
    target = getattr(config, "capture_target_long_edge", None) or \
        DEFAULTS["capture_target_long_edge"]
    cap = getattr(config, "capture_max_long_edge", None) or DEFAULTS["capture_max_long_edge"]
    upscale = getattr(config, "upscale_small_captures", True) if config else True

    original_size = img.size
    out = pixels_only(rescale(flatten(img), target, cap, upscale))
    png = to_png_bytes(out)

    info = {
        "original_size": original_size,
        "sent_size": out.size,
        "bytes": len(png),
        "scaled": out.size != original_size,
    }

    if save_debug_copy or (config and getattr(config, "debug_save_captures", False)):
        info["debug_path"] = _save_debug(png)

    return base64.b64encode(png).decode("ascii"), info


def _save_debug(png_bytes):
    """Write the exact bytes being sent, so a contaminated capture can be inspected."""
    d = os.path.expanduser("~/.cache/da-assistant/captures")
    os.makedirs(d, mode=0o700, exist_ok=True)
    # These are pictures of the screen. makedirs leaves a directory that already exists
    # as it found it, and the ones made before this were 0775.
    os.chmod(d, 0o700)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = os.path.join(d, f"capture-{stamp}.png")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(png_bytes)
    return path


def describe(info):
    """One-line summary for the confirm dialog."""
    ow, oh = info["original_size"]
    sw, sh = info["sent_size"]
    kb = info["bytes"] / 1024.0
    if info["scaled"]:
        return f"{ow}x{oh} captured, sent as {sw}x{sh} ({kb:.0f} KB PNG)"
    return f"{ow}x{oh} ({kb:.0f} KB PNG)"
