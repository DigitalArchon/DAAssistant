"""A shortcut bound in the desktop's own settings runs `--action=NAME`; with no app
running to hand it to, that used to do nothing at all, to a terminal nobody had open."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (the suite's own cache, before the app is imported)

from assistant import __main__ as entry, app as app_module, ipc  # noqa: E402


class FakeApp:
    made = []
    primary = True

    def __init__(self, initial_action=None):
        self.initial_action = initial_action
        self.is_primary = FakeApp.primary
        self.ran = False
        FakeApp.made.append(self)

    def run(self):
        self.ran = True


class WithNothingRunning(unittest.TestCase):
    def setUp(self):
        FakeApp.made, FakeApp.primary = [], True
        for patch in (mock.patch.object(app_module, "Assistant", FakeApp),
                      mock.patch.object(ipc, "is_running", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_app_starts_and_does_the_action(self):
        with mock.patch.object(ipc, "send_action", return_value=(False, "no owner")):
            self.assertEqual(entry.main(["--action", "ocr_only"]), 0)
        [made] = FakeApp.made
        self.assertEqual(made.initial_action, "ocr_only")
        self.assertTrue(made.ran)

    def test_one_started_alongside_hands_its_action_to_the_first(self):
        FakeApp.primary = False
        answers = iter([(False, "no owner"), (False, "starting"), (True, "ok")])
        with mock.patch.object(ipc, "send_action", side_effect=lambda a: next(answers)):
            self.assertEqual(entry.run_app("ocr_only", wait=0), 0)
        self.assertFalse(FakeApp.made[0].ran)

    def test_a_running_app_is_handed_it_as_before(self):
        with mock.patch.object(ipc, "send_action", return_value=(True, "ok")):
            self.assertEqual(entry.main(["--action", "ocr_only"]), 0)
        self.assertEqual(FakeApp.made, [])


class TheAppDoesItOnceItIsUp(unittest.TestCase):
    def test_the_action_waits_for_the_main_loop(self):
        source = open(app_module.__file__, encoding="utf-8").read()
        self.assertIn("GLib.idle_add(self.dispatch, initial_action)", source)


if __name__ == "__main__":
    unittest.main()
