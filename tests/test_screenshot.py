"""The capture a conversation is about is shown above it, to view and save, and is
saved beside the conversation as Markdown - and reaches the disk only then."""

import base64
import os
import shutil
import sys
import tempfile
import unittest
from io import BytesIO
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import gtkcompat  # noqa: F401
from gi.repository import Gtk
from PIL import Image

from assistant import webkitguard
from assistant.config import Config
from assistant.ui import chat, markdown_html, screenshot as screenshot_view

import test_chat_streaming as streaming
from test_image_query_model import ImageQueryCase


def png(size=(30, 12), colour=(200, 40, 40)):
    buf = BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


class TheWindowGetsTheCropAsTaken(ImageQueryCase):
    def test_not_the_copy_scaled_for_the_model(self):
        opened = self._confirm()
        shown = Image.open(BytesIO(opened["screenshot"]))
        self.assertEqual(shown.size, (40, 20))        # the model's copy is upscaled
        self.assertEqual(opened["image"], "IMAGEDATA")


class ThePanel(unittest.TestCase):
    def test_it_carries_the_picture_and_both_actions(self):
        data = png()
        html = markdown_html.screenshot_html(data)
        self.assertIn("data:image/png;base64," + base64.b64encode(data).decode(), html)
        self.assertIn('data-shot="view"', html)
        self.assertIn('data-shot="save"', html)

    def test_it_is_not_a_turn(self):
        """Streaming replaces the last .turn; the screenshot must never be it."""
        self.assertNotIn("turn", markdown_html.screenshot_html(png()))

    def test_model_output_cannot_make_its_own_buttons(self):
        html = markdown_html.render_markdown('<span data-shot="save">x</span>')
        self.assertNotIn("data-shot", html)


class WindowCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for patch in (mock.patch.object(webkitguard, "STATE_FILE",
                                        os.path.join(self.dir, "webkit-crash")),
                      mock.patch.object(webkitguard, "LOG_DIR", self.dir),
                      mock.patch.object(chat, "WebKit2", None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.out = os.path.join(self.dir, "out")
        os.mkdir(self.out)

    def _window(self, screenshot=None):
        config = Config(os.path.join(self.dir, "c.json"))
        app = type("App", (), {"config": config, "client": None,
                               "notify": lambda self, *a, **k: None})()
        window = chat.ChatWindow(app, "Query image", [
            {"role": "user", "content": "prompt", "display": "What is this?"},
            {"role": "assistant", "content": "A red box.", "model": "org/model-x"}],
            screenshot=screenshot)
        self.addCleanup(window.destroy)
        return window

    def _save(self, save, path):
        chooser = mock.Mock()
        chooser.run.return_value = Gtk.ResponseType.ACCEPT if path else \
            Gtk.ResponseType.CANCEL
        chooser.get_filename.return_value = path
        with mock.patch.object(chat.Gtk.FileChooserNative, "new", return_value=chooser):
            save()
        return chooser


class SavingAsMarkdownWithTheScreenshot(WindowCase):
    def test_the_picture_goes_beside_the_markdown_and_is_linked(self):
        data = png()
        window = self._window(data)
        path = os.path.join(self.out, "Query image 2026.md")
        self._save(lambda: window._on_save_markdown(None), path)

        picture = os.path.join(self.out, "Query image 2026.png")
        with open(picture, "rb") as f:
            self.assertEqual(f.read(), data)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith(
            "## Screenshot\n\n![Screenshot](Query%20image%202026.png)\n\n## You"), text)
        self.assertIn("A red box.", text)

    def test_copy_leaves_the_picture_out(self):
        window = self._window(png())
        self.assertNotIn("Screenshot", window._markdown())

    def test_a_conversation_without_one_saves_the_markdown_alone(self):
        window = self._window()
        self._save(lambda: window._on_save_markdown(None), os.path.join(self.out, "a.md"))
        self.assertEqual(os.listdir(self.out), ["a.md"])
        with open(os.path.join(self.out, "a.md"), encoding="utf-8") as f:
            self.assertNotIn("Screenshot", f.read())

    def test_someone_elses_picture_of_that_name_is_left_alone(self):
        theirs = os.path.join(self.out, "a.png")
        with open(theirs, "wb") as f:
            f.write(b"theirs")
        window = self._window(png())
        self._save(lambda: window._on_save_markdown(None), os.path.join(self.out, "a.md"))
        with open(theirs, "rb") as f:
            self.assertEqual(f.read(), b"theirs")
        with open(os.path.join(self.out, "a.md"), encoding="utf-8") as f:
            self.assertIn("![Screenshot](a%20%282%29.png)", f.read())
        self.assertTrue(os.path.exists(os.path.join(self.out, "a (2).png")))

    def test_replacing_a_saved_conversation_replaces_its_picture(self):
        for name, data in (("a.md", b"old"), ("a.png", b"old")):
            with open(os.path.join(self.out, name), "wb") as f:
                f.write(data)
        data = png()
        window = self._window(data)
        self._save(lambda: window._on_save_markdown(None), os.path.join(self.out, "a.md"))
        self.assertEqual(sorted(os.listdir(self.out)), ["a.md", "a.png"])
        with open(os.path.join(self.out, "a.png"), "rb") as f:
            self.assertEqual(f.read(), data)

    def test_cancelling_writes_nothing(self):
        window = self._window(png())
        self._save(lambda: window._on_save_markdown(None), None)
        self.assertEqual(os.listdir(self.out), [])


class SavingTheScreenshot(WindowCase):
    def test_it_writes_the_bytes_as_taken(self):
        data = png()
        window = self._window(data)
        chooser = self._save(window._on_save_screenshot, os.path.join(self.out, "s.png"))
        with open(os.path.join(self.out, "s.png"), "rb") as f:
            self.assertEqual(f.read(), data)
        chooser.set_do_overwrite_confirmation.assert_called_with(True)
        name = chooser.set_current_name.call_args[0][0]
        self.assertTrue(name.startswith("Query image ") and name.endswith(".png"), name)

    def test_a_failed_write_is_said_rather_than_raised(self):
        window = self._window(png())
        self._save(window._on_save_screenshot, os.path.join(self.out, "no", "s.png"))
        self.assertIn("not saved", window.status.get_text())


class TheFallbackView(WindowCase):
    def test_it_shows_the_screenshot_above_the_turns(self):
        window = self._window(png())
        first = window.bubbles.box.get_children()[0]
        labels = [w.get_text() for w in first.get_children() if isinstance(w, Gtk.Label)]
        self.assertEqual(labels, ["SCREENSHOT"])
        self.assertIn("A red box.", window.bubbles.transcript_text())

    def test_without_one_there_is_no_panel(self):
        window = self._window()
        first = window.bubbles.box.get_children()[0]
        labels = [w.get_text() for w in first.get_children() if isinstance(w, Gtk.Label)]
        self.assertNotIn("SCREENSHOT", labels)
        self.assertEqual(labels, ["YOU"])


class Viewing(WindowCase):
    def test_the_viewer_opens_from_memory(self):
        window = self._window(png((300, 200)))
        with mock.patch("builtins.open", side_effect=AssertionError("touched disk")):
            viewer = screenshot_view.ScreenshotViewer(window, window.screenshot,
                                                      "Query image", lambda _p: None)
        self.addCleanup(viewer.destroy)
        self.assertEqual(viewer.get_transient_for(), window)

    def test_a_thumbnail_fits_its_box(self):
        pixbuf = screenshot_view.load(png((4000, 50)))
        image = screenshot_view.picture(pixbuf, 1, 560, 220)
        self.assertLessEqual(image.get_preferred_width()[1], 560)


class InTheWebKitView(unittest.TestCase):
    setUpClass = streaming.StreamingDom.setUpClass
    _run_until = streaming.StreamingDom._run_until

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "test"

    def _eval(self, window, script):
        result = {}

        def got(view, res):
            try:
                result["v"] = view.run_javascript_finish(res).get_js_value().to_string()
            except Exception as e:
                result["v"] = f"ERR {e}"
            Gtk.main_quit()

        window.webview.run_javascript(script, None, got)
        Gtk.main()
        return result.get("v")

    def test_the_answer_streams_in_below_it_and_view_reaches_the_window(self):
        client = streaming.StubClient("A red box.")
        window = chat.ChatWindow(streaming.StubApp(self.config, client), "Query image",
                                 [{"role": "user", "content": "p", "display": "Q"}],
                                 screenshot=png())
        self.addCleanup(window.destroy)
        self.assertTrue(self._run_until(lambda: window._loaded), "page never loaded")
        window._start_stream()
        self.assertTrue(self._run_until(
            lambda: window._cancel is None and len(window.conversation) >= 2
            and window._loaded), "stream did not finish")

        order = self._eval(window, "Array.from(document.body.children)"
                                   ".map(function(e){return e.className||e.id}).join(',')")
        self.assertEqual(order, "shot,turn user,turn assistant,end")

        viewed = []
        window._view_screenshot = lambda: viewed.append(True) or Gtk.main_quit()
        window.webview.run_javascript(
            "document.querySelector('.shot img').click()", None, None, None)
        self._run_until(lambda: viewed, timeout=5)
        self.assertEqual(viewed, [True])


if __name__ == "__main__":
    unittest.main()
