"""Attachments, blacking out, and Ask anything.

What matters most here is what cannot be seen in the UI: that a picture leaves with its
pixels and nothing else - no EXIF, ICC profile, text chunks or trailing bytes - that a
blacked-out area is black in the bytes sent, not just on screen, and that a model that
cannot see is given a reading of each picture instead of the picture.

All pictures are made here; no real photo or screenshot is used.
"""

import base64
import os
import shutil
import struct
import sys
import tempfile
import unittest
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from gi.repository import GLib, Gtk
from PIL import Image, PngImagePlugin

from assistant import attachments, imaging, prompts
from assistant.config import DEFAULTS, Config

SECRET = b"SECRET-METADATA-do-not-send"


def png_chunks(data):
    """The chunk types in a PNG, in order."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    found, at = [], 8
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        found.append(kind.decode("ascii"))
        at += 12 + length
        if kind == b"IEND":
            break
    return found, data[at:]


def noisy_png(size=(64, 40)):
    """A PNG carrying everything that must not survive: text chunks, an ICC profile,
    EXIF, and bytes after IEND."""
    image = Image.new("RGB", size, (200, 30, 30))
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", SECRET.decode())
    info.add_itxt("XML:com.adobe.xmp", SECRET.decode())
    exif = Image.Exif()
    exif[0x010E] = SECRET.decode()                      # ImageDescription
    buf = BytesIO()
    image.save(buf, "PNG", pnginfo=info, icc_profile=SECRET * 4, exif=exif.tobytes())
    return buf.getvalue() + SECRET


def noisy_jpeg(size=(60, 30), orientation=6):
    """A JPEG with GPS, a description, a rotation, an ICC profile and a comment."""
    image = Image.new("RGB", size, (20, 120, 220))
    image.paste((255, 255, 0), (0, 0, 10, 10))          # a corner to track the rotation
    exif = Image.Exif()
    exif[0x010E] = SECRET.decode()
    exif[0x0112] = orientation
    exif[0x8825] = {1: "S", 2: (33.0, 52.0, 0.0)}        # GPS
    buf = BytesIO()
    image.save(buf, "JPEG", exif=exif.tobytes(), icc_profile=SECRET * 4,
               comment=SECRET)
    return buf.getvalue()


class PixelsOnly(unittest.TestCase):
    def test_png_sent_for_a_capture_has_no_metadata(self):
        source = Image.open(BytesIO(noisy_png()))
        source.load()
        self.assertIn("icc_profile", source.info)       # the test means something
        b64, _info = imaging.prepare_for_vision(source)
        data = base64.b64decode(b64)
        chunks, trailing = png_chunks(data)
        self.assertEqual(set(chunks) - {"IHDR", "IDAT", "IEND"}, set())
        self.assertEqual(trailing, b"")
        self.assertNotIn(SECRET, data)

    def test_screenshot_kept_for_the_window_has_no_metadata(self):
        source = Image.open(BytesIO(noisy_png()))
        source.load()
        data = imaging.screenshot_png(source)
        self.assertEqual(set(png_chunks(data)[0]) - {"IHDR", "IDAT", "IEND"}, set())
        self.assertNotIn(SECRET, data)


class BlackOut(unittest.TestCase):
    def test_boxes_are_solid_black_and_nothing_else_changes(self):
        image = Image.new("RGB", (50, 30), (250, 250, 250))
        out = imaging.black_out(image, [(10, 5, 20, 10)])
        self.assertEqual(out.getpixel((10, 5)), (0, 0, 0))
        self.assertEqual(out.getpixel((29, 14)), (0, 0, 0))
        self.assertEqual(out.getpixel((30, 14)), (250, 250, 250))
        self.assertEqual(out.getpixel((9, 5)), (250, 250, 250))
        self.assertEqual(image.getpixel((10, 5)), (250, 250, 250))  # a copy

    def test_boxes_are_clipped_and_transparency_flattened(self):
        image = Image.new("RGBA", (20, 20), (255, 0, 0, 0))
        out = imaging.black_out(image, [(15, 15, 50, 50), (-5, -5, 8, 8)])
        self.assertEqual(out.mode, "RGB")
        self.assertEqual(out.getpixel((19, 19)), (0, 0, 0))
        self.assertEqual(out.getpixel((0, 0)), (0, 0, 0))
        self.assertEqual(out.getpixel((2, 2)), (0, 0, 0))
        self.assertEqual(out.getpixel((3, 3)), (255, 255, 255))
        self.assertEqual(imaging.black_out(image, [(-5, -5, 3, 3)]).getpixel((0, 0)),
                         (255, 255, 255))          # wholly off the picture: nothing

    def test_blacked_out_area_is_black_in_what_is_sent(self):
        image = Image.new("RGB", (400, 200), (255, 255, 255))
        image.paste((200, 0, 0), (100, 50, 300, 150))     # the "secret"
        out = imaging.black_out(image, [(100, 50, 200, 100)])
        b64, _ = imaging.prepare_for_vision(out)
        sent = Image.open(BytesIO(base64.b64decode(b64))).convert("RGB")
        # Scaled for the model, but nothing red is left anywhere.
        reds = [p for p in sent.getdata() if p[0] > 100 and p[1] < 80]
        self.assertEqual(reds, [])


class Pictures(unittest.TestCase):
    def test_jpeg_is_turned_upright_and_stripped(self):
        image, photo = attachments.sniff_image(noisy_jpeg())
        self.assertTrue(photo)
        self.assertEqual(image.size, (30, 60))              # orientation 6: rotated
        self.assertEqual(image.info, {})
        data, mime = attachments.encode_image(image, photo)
        self.assertEqual(mime, "image/jpeg")
        self.assertNotIn(SECRET, data)
        self.assertNotIn(b"Exif", data)
        self.assertNotIn(b"ICC_PROFILE", data)
        with Image.open(BytesIO(data)) as sent:
            self.assertEqual(len(sent.getexif()), 0)
            self.assertNotIn("icc_profile", sent.info)
            self.assertNotIn("comment", sent.info)

    def test_png_loses_text_icc_exif_and_trailing_bytes(self):
        image, photo = attachments.sniff_image(noisy_png())
        self.assertFalse(photo)
        attachment = attachments.image_attachment("shot.png", image, photo)
        data = base64.b64decode(attachment["base64"])
        self.assertEqual(attachment["mime"], "image/png")
        chunks, trailing = png_chunks(data)
        self.assertEqual(set(chunks) - {"IHDR", "IDAT", "IEND"}, set())
        self.assertEqual(trailing, b"")
        self.assertNotIn(SECRET, data)
        self.assertNotIn(SECRET, attachment["thumb"])

    def test_webp_gif_and_bmp_are_re_encoded_as_png(self):
        for fmt in ("WEBP", "GIF", "BMP"):
            buf = BytesIO()
            Image.new("RGB", (20, 10), (0, 128, 0)).save(buf, fmt)
            image, photo = attachments.sniff_image(buf.getvalue())
            _data, mime = attachments.encode_image(image, photo)
            self.assertEqual(mime, "image/png", fmt)

    def test_only_the_first_frame_of_an_animation(self):
        frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 0, 255))]
        buf = BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
        image, _ = attachments.sniff_image(buf.getvalue())
        self.assertEqual(image.getpixel((0, 0))[0], 255)

    def test_large_pictures_are_scaled_down_to_the_cap(self):
        image = Image.new("RGB", (5000, 1000), (1, 2, 3))
        data, _ = attachments.encode_image(image)
        with Image.open(BytesIO(data)) as sent:
            self.assertEqual(max(sent.size), DEFAULTS["capture_max_long_edge"])

    def test_refused_pictures(self):
        buf = BytesIO()
        Image.new("RGB", (4, 4)).save(buf, "TIFF")
        with self.assertRaises(attachments.AttachmentError):
            attachments.sniff_image(buf.getvalue())
        buf = BytesIO()
        Image.new("1", (attachments.MAX_SIDE + 1, 1)).save(buf, "PNG")
        with self.assertRaises(attachments.AttachmentError):
            attachments.sniff_image(buf.getvalue())

    def test_text_is_not_a_picture(self):
        self.assertIsNone(attachments.sniff_image(b'{"a": 1}'))


class TextFiles(unittest.TestCase):
    def test_decodes_utf8_with_or_without_bom_and_utf16(self):
        self.assertEqual(attachments.decode_text("héllo\r\nworld".encode()), "héllo\nworld")
        self.assertEqual(attachments.decode_text(b"\xef\xbb\xbfhi"), "hi")
        self.assertEqual(attachments.decode_text("hi ✓".encode("utf-16")), "hi ✓")

    def test_refuses_binary_and_oversized(self):
        for raw in (b"abc\x00def", b"\xff\xfe\xfd not utf8 \xc3"):
            with self.assertRaises(attachments.AttachmentError):
                attachments.decode_text(raw[:3] if raw.startswith(b"\xff\xfe") else raw)
        with self.assertRaises(attachments.AttachmentError):
            attachments.decode_text(b"\xc3\x28")
        with self.assertRaises(attachments.AttachmentError):
            attachments.decode_text(b"a" * (attachments.MAX_TEXT_BYTES + 1))

    def test_fence_outlasts_backticks_in_the_file(self):
        block = attachments.fenced("notes.md", "```py\nx\n```\n````\n")
        first, *_middle, last = block.split("\n")
        self.assertEqual(first, "Attached file: notes.md")
        self.assertTrue(block.split("\n")[1].startswith("`````markdown"))
        self.assertEqual(last, "`````")

    def test_language_comes_only_from_a_plain_extension(self):
        self.assertIn("\n```json\n", attachments.fenced("a.json", "{}"))
        self.assertIn("\n```\n", attachments.fenced("weird.n`ame", "x"))


class Composing(unittest.TestCase):
    def setUp(self):
        image = Image.new("RGB", (30, 20), (9, 9, 9))
        self.picture = attachments.image_attachment("pic.png", image)
        self.notes = attachments.text_attachment("notes.md", "# Title\nbody")

    def test_turn_carries_text_files_in_content_and_pictures_aside(self):
        turn = attachments.compose("What is this?", [self.notes, self.picture])
        self.assertEqual(turn["display"], "What is this?")
        self.assertIn("Attached file: notes.md", turn["content"])
        self.assertIn("# Title\nbody", turn["content"])
        self.assertNotIn(self.picture["base64"], turn["content"])
        self.assertEqual([i["name"] for i in turn["images"]], ["pic.png"])
        self.assertEqual([a["name"] for a in turn["attachments"]], ["notes.md", "pic.png"])

    def test_pictures_alone_still_ask_something(self):
        turn = attachments.compose("", [self.picture])
        self.assertEqual(turn["content"], prompts.PICTURES_ONLY)

    def test_a_model_that_sees_gets_image_parts(self):
        turn = attachments.compose("Q", [self.picture])
        [message] = attachments.to_messages([turn], can_see=True)
        self.assertNotIn("images", message)
        image, text = message["content"]          # the image first: see llm.attach_image
        self.assertEqual(text, {"type": "text", "text": "Q"})
        self.assertEqual(image["image_url"]["url"],
                         "data:image/png;base64," + self.picture["base64"])

    def test_a_model_that_cannot_see_gets_the_reading(self):
        turn = attachments.compose("Q", [self.picture])
        turn["images"][0]["reading"] = "## Text\nNO TEXT FOUND"
        [message] = attachments.to_messages([turn], can_see=False)
        self.assertIsInstance(message["content"], str)
        self.assertIn("pic.png", message["content"])
        self.assertIn("NO TEXT FOUND", message["content"])
        self.assertNotIn(self.picture["base64"], message["content"])

    def test_history_keeps_pictures_when_unanswered_turns_merge(self):
        from assistant.ui.chat import history_for
        first = attachments.compose("one", [self.picture])
        second = attachments.compose("two", [attachments.image_attachment(
            "b.png", Image.new("RGB", (5, 5)))])
        [merged] = history_for([first, second])
        self.assertEqual([i["name"] for i in merged["images"]], ["pic.png", "b.png"])
        merged["images"][0]["reading"] = "x"
        self.assertNotIn("reading", first["images"][0])     # copies, for the worker


def run_until(predicate, timeout=20):
    expired = []

    def expire():
        expired.append(True)
        Gtk.main_quit()
        return False

    source = GLib.timeout_add_seconds(timeout, expire)

    def poll():
        if predicate():
            Gtk.main_quit()
            return False
        return True

    GLib.timeout_add(50, poll)
    Gtk.main()
    if not expired:
        GLib.source_remove(source)
    return not expired


class RedactEditor(unittest.TestCase):
    def setUp(self):
        from assistant.ui.redact import RedactDialog
        self.results = []
        self.image = Image.new("RGB", (100, 60), (255, 255, 255))
        self.dialog = RedactDialog(None, self.image,
                                   lambda image, boxes: self.results.append((image, boxes)))

    def tearDown(self):
        if not self.dialog._finished:
            self.dialog.destroy()

    def test_boxes_snap_outwards_and_apply_paints_them(self):
        self.assertTrue(self.dialog.add_box(10.6, 5.2, 40.1, 20.9))
        self.assertEqual(self.dialog.boxes, [(10, 5, 31, 16)])
        self.assertFalse(self.dialog.add_box(1, 1, 2.5, 30))     # a click, not a box
        self.dialog.apply()
        [(image, boxes)] = self.results
        self.assertEqual(boxes, [(10, 5, 31, 16)])
        self.assertEqual(image.getpixel((10, 5)), (0, 0, 0))
        self.assertEqual(image.getpixel((40, 20)), (0, 0, 0))
        self.assertEqual(image.getpixel((41, 20)), (255, 255, 255))

    def test_undo_clear_and_cancel(self):
        self.dialog.add_box(0, 0, 10, 10)
        self.dialog.add_box(20, 20, 30, 30)
        self.dialog.undo()
        self.assertEqual(len(self.dialog.boxes), 1)
        self.dialog.clear()
        self.assertEqual(self.dialog.boxes, [])
        self.dialog.cancel()
        self.assertEqual(self.results, [(None, [])])

    def test_closing_the_window_cancels_once(self):
        self.dialog.add_box(0, 0, 10, 10)
        self.dialog.destroy()
        self.assertEqual(self.results, [(None, [])])

    def test_dragging_in_fit_mode_maps_to_image_pixels(self):
        self.dialog.resize(700, 500)
        self.assertTrue(run_until(lambda: self.dialog.area.get_allocated_width() > 200))
        zoom, ox, oy = self.dialog._view()
        press = type("E", (), {"button": 1, "x": ox + 20 * zoom, "y": oy + 10 * zoom})
        move = type("E", (), {"button": 1, "x": ox + 50 * zoom, "y": oy + 30 * zoom})
        self.dialog._on_press(None, press)
        self.dialog._on_motion(None, move)
        self.dialog._on_release(None, move)
        [(x, y, w, h)] = self.dialog.boxes
        self.assertAlmostEqual(x, 20, delta=1)
        self.assertAlmostEqual(y, 10, delta=1)
        self.assertAlmostEqual(w, 30, delta=2)
        self.assertAlmostEqual(h, 20, delta=2)


class CaptureReviewBlackOut(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.sent = {}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_blacked_out_capture_is_what_is_sent_and_shown(self):
        from assistant.ui import dialogs
        made = []

        class Editor:
            def __init__(self, parent, image, on_done, **_kw):
                made.append(self)
                self.on_done, self.image = on_done, image

        import assistant.ui.redact as redact_module
        real = redact_module.RedactDialog
        redact_module.RedactDialog = Editor
        try:
            image = Image.new("RGB", (120, 60), (255, 0, 0))
            dialog = dialogs.CaptureReviewDialog(
                image, "Query image", self.config,
                lambda img, b64, info, prompt, ws: self.sent.update(image=img, b64=b64),
                ask_prompt=True)
            dialog.prompt_view.get_buffer().set_text("what?")
            dialog._on_redact()
            [editor] = made
            editor.on_done(imaging.black_out(editor.image, [(0, 0, 120, 60)]),
                           [(0, 0, 120, 60)])
            self.assertIn("1 area blacked out", dialog.meta.get_text())
            dialog._confirm()
        finally:
            redact_module.RedactDialog = real
        self.assertEqual(self.sent["image"].getpixel((5, 5)), (0, 0, 0))
        sent = Image.open(BytesIO(base64.b64decode(self.sent["b64"]))).convert("RGB")
        self.assertEqual(set(sent.getdata()), {(0, 0, 0)})


class RecordingClient:
    def __init__(self):
        self.streamed, self.read = [], []

    def complete(self, model, messages, image_base64=None, cancel=None,
                 web_search=None, system=None):
        self.read.append({"model": model, "messages": messages})
        return "## Text\nA RED SQUARE"

    def stream(self, model, messages, image_base64=None, cancel=None, on_delta=None,
               web_search=None, system=None):
        self.streamed.append({"model": model, "messages": messages})
        return "An answer."


class ChatAttachments(unittest.TestCase):
    """Files attached in a chat window and sent, against a recording client."""

    def setUp(self):
        from assistant.ui import chat
        self.chat = chat
        self.dir = tempfile.mkdtemp()
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"
        self.config.web_search_enabled = False
        self.client = RecordingClient()
        self.sees = True
        self._accepts = chat.catalogue.accepts_images
        chat.catalogue.accepts_images = lambda _c, _m: self.sees
        app = type("App", (), {"config": self.config, "client": self.client,
                               "notify": lambda self, *a, **k: None})()
        self.window = chat.ChatWindow(app, "Ask anything", [])
        # The black-out editor, answered at once: Attach, with nothing blacked out.
        self.window._redact = lambda image, done, name: done(image, [])
        self.png = os.path.join(self.dir, "photo.png")
        with open(self.png, "wb") as f:
            f.write(noisy_png())
        self.md = os.path.join(self.dir, "notes.md")
        with open(self.md, "w", encoding="utf-8") as f:
            f.write("# Notes\nsecond line\n")
        self.bin = os.path.join(self.dir, "blob.bin")
        with open(self.bin, "wb") as f:
            f.write(b"\x00\x01\x02")

    def tearDown(self):
        self.chat.catalogue.accepts_images = self._accepts
        self.window.destroy()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _send(self, text):
        self.window.entry.get_buffer().set_text(text)
        self.window._send()
        self.assertTrue(run_until(lambda: self.window._cancel is None
                                  and self.client.streamed))

    def test_attach_then_send_to_a_model_that_sees(self):
        self.window.attach_files([self.png, self.md, self.bin])
        self.assertEqual([a["name"] for a in self.window._attachments],
                         ["photo.png", "notes.md"])
        self.assertIn("blob.bin", self.window.status.get_text())
        self.assertTrue(self.window.attachment_bar.get_visible())
        self._send("What do these show?")
        self.assertEqual(self.window._attachments, [])
        self.assertFalse(self.window.attachment_bar.get_visible())
        [user] = [m for m in self.client.streamed[0]["messages"] if m["role"] == "user"]
        picture, text = user["content"]
        self.assertIn("What do these show?", text["text"])
        self.assertIn("# Notes\nsecond line", text["text"])
        sent = base64.b64decode(picture["image_url"]["url"].split(",", 1)[1])
        self.assertNotIn(SECRET, sent)
        self.assertEqual(self.client.read, [])
        turn = self.window.conversation[0]
        self.assertEqual(turn["display"], "What do these show?")
        self.assertIn("photo.png", self.window._markdown())

    def test_a_model_that_cannot_see_is_given_one_reading_per_picture(self):
        self.sees = False
        self.window.attach_files([self.png])
        self._send("Describe it")
        [read] = self.client.read
        self.assertEqual(read["model"], self.config.vision_model)
        [user] = [m for m in self.client.streamed[0]["messages"] if m["role"] == "user"]
        self.assertIsInstance(user["content"], str)
        self.assertIn("A RED SQUARE", user["content"])
        self.assertTrue(run_until(
            lambda: self.window.conversation[0]["images"][0].get("reading")))
        # A follow-up does not read the same picture again.
        self.client.streamed.clear()
        self._send("And again?")
        self.assertEqual(len(self.client.read), 1)


class Transcript(unittest.TestCase):
    def test_file_names_are_escaped_and_thumbnails_inline(self):
        from assistant.ui import markdown_html
        picture = attachments.image_attachment('<img src=x onerror=alert(1)>.png',
                                               Image.new("RGB", (8, 8)))
        turn = attachments.compose("", [picture])
        html = markdown_html.turn_html("user", turn["display"],
                                       attachments=turn["attachments"])
        self.assertNotIn("<img src=x", html)
        self.assertIn('src="data:image/png;base64,', html)
        self.assertNotIn("(empty response)", html)


class AskAnything(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_tray_menu_starts_with_ask_anything(self):
        from assistant.app import Assistant

        class Hotkeys:
            mode = "static"

        stub = type("A", (), {"hotkeys": Hotkeys(), "dispatch": lambda self, n: None,
                              "config": Config(os.path.join(self.dir, "c.json")),
                              "_on_premium_toggled": lambda *a: None,
                              "_on_privacy_toggled": lambda *a: None,
                              "_sync_encyclopedia_items": lambda self: None})()
        menu = Assistant._build_menu(stub)
        first = menu.get_children()[0]
        self.assertEqual(first.get_label(), "Ask anything")

    def test_action_is_known_to_the_command_line(self):
        from assistant.__main__ import build_parser
        self.assertEqual(build_parser().parse_args(["--action=ask_anything"]).action,
                         "ask_anything")


if __name__ == "__main__":
    unittest.main(verbosity=2)
