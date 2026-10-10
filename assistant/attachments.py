"""Files attached to a chat message: pictures, and text of any kind.

Pictures are decoded to pixels and re-encoded from those pixels alone, as DAToolkit does
(safety/images.py there). Whatever the file carried, only what is visible survives: no
EXIF (GPS, camera, timestamps, the embedded thumbnail), no XMP, ICC profile, PNG text
chunks or JPEG comments, no frames after the first, no bytes appended after the image. A
photo is turned upright from its EXIF orientation first, so it is sent the way it looks.
The result is PNG, or JPEG for a photo that was one, and it is what the user saw in the
black-out editor (ui/redact.py) when they attached it.

Text is anything that decodes as UTF-8 (or UTF-16 with a byte-order mark) and has no NUL
bytes: Markdown, JSON, CSV, logs, source code. It goes into the message as a fenced
block under its file name; nothing of the file but its name and its text is sent.

What a turn carries, and how a request is built from it, is here too, away from GTK, so
it can be tested on its own: see to_messages.
"""

import base64
import itertools
import os
import re
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from . import imaging, prompts

# Decoded here, re-encoded here: the formats Pillow reads without a plugin, and that
# people actually have pictures in.
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP")
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_PIXELS = 40_000_000             # refuse decompression bombs before decoding
MAX_SIDE = 10_000
MAX_TEXT_BYTES = 512 * 1024
THUMBNAIL_SIDE = 160
JPEG_QUALITY = 90

# The file chooser's filter. What is accepted is decided by content, not by name: this
# only saves scrolling past what will be refused anyway.
TEXT_PATTERNS = ("*.txt", "*.md", "*.markdown", "*.json", "*.jsonl", "*.csv", "*.tsv",
                 "*.yaml", "*.yml", "*.toml", "*.ini", "*.cfg", "*.conf", "*.xml",
                 "*.html", "*.htm", "*.log", "*.py", "*.js", "*.ts", "*.sh", "*.c",
                 "*.h", "*.cpp", "*.rs", "*.go", "*.java", "*.sql", "*.diff", "*.patch")
IMAGE_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.bmp")

_ids = itertools.count(1)


class AttachmentError(ValueError):
    """A file that cannot be attached; the message says why, for the status line."""


def read_file(path):
    """The file's bytes, refusing anything too large before reading it."""
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            raise AttachmentError(f"{os.path.basename(path)} is {size // (1024 * 1024)} "
                                  f"MB; the limit is {MAX_FILE_BYTES // (1024 * 1024)} MB")
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise AttachmentError(f"{os.path.basename(path)}: {e.strerror or e}") from e


def sniff_image(raw):
    """(image, photo) for the picture in `raw` - decoded, upright, flattened to RGB, and
    whether it was a JPEG - or None when `raw` is not a picture at all. Raises
    AttachmentError for one that cannot be used."""
    try:
        im = Image.open(BytesIO(raw))
    except UnidentifiedImageError:
        return None
    except (OSError, Image.DecompressionBombError, SyntaxError) as e:
        raise AttachmentError(f"not a readable picture ({e})") from e
    with im:
        if im.format not in IMAGE_FORMATS:
            raise AttachmentError(f"{im.format} pictures are not supported; use PNG, "
                                  f"JPEG, WebP, GIF or BMP")
        w, h = im.size
        if max(w, h) > MAX_SIDE or w * h > MAX_PIXELS:
            raise AttachmentError(f"the picture is {w}x{h}, larger than this app accepts")
        try:
            im.seek(0)                                  # first frame only
            pixels = imaging.flatten(ImageOps.exif_transpose(im))
            pixels.load()
        except (OSError, Image.DecompressionBombError, SyntaxError, ValueError) as e:
            raise AttachmentError(f"not a readable picture ({e})") from e
        photo = im.format == "JPEG"
    return imaging.pixels_only(pixels), photo


def encode_image(image, photo=False, config=None):
    """(bytes, mime) for a picture to send: pixels only, no larger than the capture cap,
    PNG - lossless, for screenshots and diagrams - or JPEG for a photo that was one."""
    from .config import DEFAULTS
    cap = getattr(config, "capture_max_long_edge", None) or \
        DEFAULTS["capture_max_long_edge"]
    out = imaging.pixels_only(imaging.rescale(imaging.flatten(image), cap, cap,
                                              upscale=False))
    buf = BytesIO()
    if photo:
        out.save(buf, "JPEG", quality=JPEG_QUALITY)
        return buf.getvalue(), "image/jpeg"
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue(), "image/png"


def thumbnail_png(image, side=THUMBNAIL_SIDE):
    small = imaging.flatten(image).copy()
    small.thumbnail((side, side), Image.LANCZOS)
    return imaging.to_png_bytes(imaging.pixels_only(small))


def decode_text(raw, name="file"):
    """The text in `raw`, or AttachmentError for something that is not text."""
    if len(raw) > MAX_TEXT_BYTES:
        raise AttachmentError(f"{name} is {len(raw) // 1024} KB of text; the limit is "
                              f"{MAX_TEXT_BYTES // 1024} KB")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError as e:
            raise AttachmentError(f"{name} is not readable text") from e
    else:
        if b"\x00" in raw:
            raise AttachmentError(f"{name} is not a text file or a picture")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise AttachmentError(f"{name} is not UTF-8 text or a picture") from e
    return text.replace("\r\n", "\n").replace("\r", "\n")


def image_attachment(name, image, photo=False, config=None):
    """An attached picture, from the image the black-out editor returned."""
    data, mime = encode_image(image, photo, config)
    with Image.open(BytesIO(data)) as sent:
        size = sent.size                # after any scaling down: what the model gets
    return {"kind": "image", "id": next(_ids), "name": name, "mime": mime,
            "base64": base64.b64encode(data).decode("ascii"), "bytes": len(data),
            "size": size, "thumb": thumbnail_png(image)}


def text_attachment(name, text):
    return {"kind": "text", "id": next(_ids), "name": name, "text": text,
            "bytes": len(text.encode("utf-8"))}


def describe(attachment):
    """'notes.md · 3 KB' - for chips and the transcript."""
    size = attachment["bytes"]
    amount = f"{size / 1024:.0f} KB" if size >= 1024 else f"{size} bytes"
    if attachment["kind"] == "image":
        w, h = attachment["size"]
        return f"{attachment['name']} · {w}x{h}, {amount}"
    return f"{attachment['name']} · {amount}"


def fenced(name, text):
    """A text file as the model is given it: its name, then its text in a fence longer
    than any run of backticks inside it, so the file cannot close the block early."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    language = os.path.splitext(name)[1].lstrip(".").lower()
    language = {"md": "markdown", "yml": "yaml", "htm": "html"}.get(language, language)
    if not re.fullmatch(r"[a-z0-9+_-]{1,20}", language or ""):
        language = ""
    return f"Attached file: {name}\n{fence}{language}\n{text.rstrip(chr(10))}\n{fence}"


def compose(text, attached):
    """The turn for a message sent with attachments: {content, display, attachments,
    images}. `content` is the message and every text file, fenced; `images` the
    pictures, which go with the turn as image parts (to_messages); `attachments` what
    the transcript shows of them all - names and thumbnails, never the text again."""
    files = [a for a in attached if a["kind"] == "text"]
    pictures = [a for a in attached if a["kind"] == "image"]
    content = "\n\n".join([text] * bool(text) + [fenced(a["name"], a["text"])
                                                 for a in files])
    if not text and pictures and not files:
        content = prompts.PICTURES_ONLY
    turn = {"role": "user", "content": content, "display": text,
            "attachments": [{"kind": a["kind"], "name": a["name"],
                             "label": describe(a), "thumb": a.get("thumb")}
                            for a in attached]}
    if pictures:
        turn["images"] = [{"id": a["id"], "name": a["name"], "mime": a["mime"],
                           "base64": a["base64"]} for a in pictures]
    return turn


def unread(history):
    """The pictures in `history` that no vision model has read yet."""
    return [image for turn in history for image in turn.get("images") or ()
            if not image.get("reading")]


def reading_request(image):
    """The request for the vision model's reading of one attached picture."""
    return [{"role": "user", "content": [
        {"type": "text", "text": prompts.IMAGE_BRIEF},
        {"type": "image_url",
         "image_url": {"url": f"data:{image['mime']};base64,{image['base64']}"}}]}]


def to_messages(history, can_see):
    """`history` (chat.history_for) with each turn's pictures put where the model will
    find them: as image parts, for a model that can see; else as the vision model's
    reading of each, appended to the turn's text. Turns without pictures are unchanged.
    """
    out = []
    for turn in history:
        images = turn.get("images")
        entry = {k: v for k, v in turn.items() if k != "images"}
        if images:
            text = entry.get("content") or ""
            if can_see:
                entry["content"] = [{"type": "text", "text": text}] + [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{i['mime']};base64,{i['base64']}"}}
                    for i in images]
            else:
                readings = [prompts.attached_reading(i["name"], i.get("reading") or
                                                     "(not read)") for i in images]
                entry["content"] = "\n\n".join([text] * bool(text) + readings)
        out.append(entry)
    return out
