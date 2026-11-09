"""Small things in the windows that got in the way: a status line that widened the window
past the screen, a dialog that could not be sent from the keyboard, Save greyed with
its reason out of sight, colours too faint to read, a window that forgot its size."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import gtkcompat  # noqa: F401,E402
from gi.repository import Gdk, Gtk  # noqa: E402

from assistant import errors, webkitguard  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.ui import chat, keys, tones  # noqa: E402
from assistant.ui.dialogs import SettingsDialog, TextReviewDialog  # noqa: E402

from test_chat_requests import Scripted, StubApp, run_until  # noqa: E402


def key(keyval, state=0):
    # The event itself, not its .key: that points into the event, which is freed with
    # the last reference to it.
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.key.keyval = keyval
    event.key.state = state
    return event


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for patch in (mock.patch.object(webkitguard, "STATE_FILE",
                                        os.path.join(self.dir, "webkit-crash")),
                      mock.patch.object(webkitguard, "LOG_DIR", self.dir),
                      mock.patch.object(chat, "WebKit2", None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.config = Config(os.path.join(self.dir, "c.json"))
        self.config.api_key = "k"

    def window(self, turns=(), client=None, **kwargs):
        window = chat.ChatWindow(StubApp(self.config, client or Scripted()),
                                 "Ask anything", [dict(t) for t in turns], **kwargs)
        self.addCleanup(lambda: window._destroyed or window.destroy())
        return window


class TheStatusLine(Case):
    def test_a_long_status_does_not_widen_the_window(self):
        window = self.window()
        window.show_all()
        before = window.get_preferred_width().minimum_width
        window.status.set_text("A dictionary was hidden in this conversation, and left "
                               "out of what questions are sent with; its button in the "
                               "bar brings it back " * 4)
        self.assertLessEqual(window.get_preferred_width().minimum_width, before + 40)
        self.assertIn("brings it back", window.status.get_tooltip_text())

    def test_copy_all_says_so_in_the_window_not_as_a_notification(self):
        window = self.window([{"role": "user", "content": "q"},
                              {"role": "assistant", "content": "a"}])
        notices = []
        window.app.notify = lambda *a, **k: notices.append(a)
        window._on_copy_all(None)
        self.assertEqual(notices, [])
        self.assertIn("copied", window.status.get_text())


class AskAgainWith(Case):
    def test_it_is_offered_only_where_something_was_asked(self):
        window = self.window()
        self.assertFalse(window.again_button.get_sensitive())
        answered = self.window([{"role": "user", "content": "q"},
                                {"role": "assistant", "content": "a"}])
        answered._render(full=True)
        self.assertTrue(answered.again_button.get_sensitive())


class SendingFromTheKeyboard(Case):
    def test_ctrl_enter_sends_a_review_with_no_question_box(self):
        sent = []
        dialog = TextReviewDialog("Guten Morgen", "Translate text",
                                  lambda *a: sent.append(a), config=self.config)
        self.addCleanup(lambda: dialog.get_realized() and dialog.destroy())
        self.assertTrue(dialog._on_window_key(dialog, key(
            Gdk.KEY_Return, Gdk.ModifierType.CONTROL_MASK)))
        self.assertEqual(len(sent), 1)

    def test_enter_alone_is_left_to_the_text_being_reviewed(self):
        sent = []
        dialog = TextReviewDialog("Guten Morgen", "Translate text",
                                  lambda *a: sent.append(a), config=self.config)
        self.addCleanup(dialog.destroy)
        self.assertFalse(dialog._on_window_key(dialog, key(Gdk.KEY_Return)))
        self.assertEqual(sent, [])

    def test_a_key_the_input_method_takes_is_not_also_the_app_s(self):
        view = Gtk.TextView()
        event = key(Gdk.KEY_Return)
        self.assertFalse(keys.input_method_takes(view, event))   # no window: made up
        self.assertFalse(keys.input_method_takes(Gtk.Entry(), event))
        sent = []
        dialog = TextReviewDialog("text", "Query text", lambda *a: sent.append(a),
                                  ask_prompt=True, config=self.config)
        self.addCleanup(lambda: dialog.get_realized() and dialog.destroy())
        dialog.prompt_view.get_buffer().set_text("What?")
        with mock.patch.object(keys, "input_method_takes", return_value=True):
            self.assertTrue(dialog._on_key(dialog.prompt_view, key(Gdk.KEY_Return)))
        self.assertEqual(sent, [], "a candidate committed is not the question sent")


class Settings(Case):
    def dialog(self):
        dialog = SettingsDialog(None, self.config, type("C", (), {"config": self.config}))
        self.addCleanup(dialog.destroy)
        return dialog

    def test_save_held_by_a_page_not_open_says_where(self):
        dialog = self.dialog()
        dialog.proxy_entry.set_text("ftp://nowhere")
        self.assertFalse(dialog.save_button.get_sensitive())
        self.assertTrue(dialog.save_held.get_visible())
        self.assertIn("Advanced", dialog.save_held.get_text())
        dialog.proxy_entry.set_text("")
        self.assertTrue(dialog.save_button.get_sensitive())
        self.assertFalse(dialog.save_held.get_visible())

    def test_search_settings_apply_whether_or_not_it_is_on_by_default(self):
        dialog = self.dialog()
        dialog.search_enabled.set_active(False)
        self.assertTrue(dialog.search_craft.get_sensitive())
        self.assertTrue(dialog.search_combos["web_search_provider"][0].get_sensitive())
        dialog.search_combos["web_search_mode"][0].set_active_id("off")
        self.assertFalse(dialog.search_craft.get_sensitive())
        self.assertFalse(dialog.search_combos["web_search_provider"][0].get_sensitive())
        self.assertTrue(dialog.search_combos["web_search_mode"][0].get_sensitive())

    def test_the_actions_page_scrolls_rather_than_growing_off_the_screen(self):
        dialog = self.dialog()
        notebook = next(c for c in dialog.get_content_area().get_children()
                        if isinstance(c, Gtk.Notebook))
        pages = [notebook.get_nth_page(i) for i in range(notebook.get_n_pages())]
        labels = [notebook.get_tab_label_text(page) for page in pages]
        self.assertIsInstance(pages[labels.index("Actions")], Gtk.ScrolledWindow)


class Colours(unittest.TestCase):
    def test_each_status_colour_reads_on_its_theme(self):
        def luminance(colour):
            channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                      for c in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def contrast(a, b):
            high, low = sorted((luminance(a), luminance(b)), reverse=True)
            return (high + 0.05) / (low + 0.05)

        for tone in ("ok", "warn", "bad"):
            self.assertGreaterEqual(contrast(tones.LIGHT[tone], "#f6f5f4"), 4.5, tone)
            self.assertGreaterEqual(contrast(tones.DARK[tone], "#353535"), 4.5, tone)
        self.assertIn(tones.DARK["bad"], tones.css(True))
        self.assertIn(".warning-text", tones.css(False))


class TheWindowRemembers(Case):
    def test_the_size_it_was_left_at(self):
        self.config.chat_width, self.config.chat_height = 1000, 640
        window = self.window()
        self.assertEqual(window._initial_size(), (1000, 640))
        window.resize(900, 600)
        window.show_all()
        run_until(lambda: window.get_size()[0] == 900, timeout=3)
        window._save_size()
        self.assertEqual((self.config.chat_width, self.config.chat_height),
                         tuple(window.get_size()))

    def test_never_larger_than_the_screen(self):
        self.config.chat_width, self.config.chat_height = 100000, 100000
        width, height = self.window()._initial_size()
        area = Gdk.Display.get_default().get_monitor(0).get_workarea()
        self.assertLessEqual(width, area.width)
        self.assertLessEqual(height, area.height)

    def test_the_folder_it_last_saved_to(self):
        saved = os.path.join(self.dir, "saved")
        os.mkdir(saved)
        window = self.window()
        folders = []

        class Native:
            def __init__(self, *a):
                pass

            def set_do_overwrite_confirmation(self, _v):
                pass

            def set_current_name(self, _n):
                pass

            def set_current_folder(self, folder):
                folders.append(folder)

            def add_filter(self, _f):
                pass

            def run(self):
                return Gtk.ResponseType.ACCEPT

            def get_filename(self):
                return os.path.join(saved, "talk.md")

            def destroy(self):
                pass

        self.addCleanup(setattr, chat.ChatWindow, "_last_save_folder", None)
        with mock.patch.object(chat.Gtk.FileChooserNative, "new",
                               side_effect=lambda *a: Native()):
            window._ask_save_path("Save", "talk.md", "Markdown", "*.md", "text/markdown")
            window._ask_save_path("Save", "talk.md", "Markdown", "*.md", "text/markdown")
        self.assertEqual(folders[-1], saved)

    def test_a_theme_switched_redraws_the_transcript(self):
        window = self.window([{"role": "user", "content": "q"}])
        with mock.patch.object(chat, "_is_dark", return_value=not window.dark), \
                mock.patch.object(window, "_render") as render:
            window._on_theme_changed()
        render.assert_called_once_with(full=True, new_page=True)


class Errors(unittest.TestCase):
    def test_what_went_wrong_in_words(self):
        self.assertEqual(errors.describe(OSError("No space left on device")),
                         "No space left on device")
        described = errors.describe(KeyError("senses"))
        self.assertNotIn("'senses'", described)
        self.assertIn("KeyError", described)
        self.assertIn(errors.LOG_FILE, described)


if __name__ == "__main__":
    unittest.main(verbosity=2)
