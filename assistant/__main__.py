"""Command-line entry point.

  run-assistant                      start the tray app
  run-assistant --action=ocr_only    tell the running app to do something
                                     (this is what the hotkey bindings invoke)
  run-assistant --probe              print what this machine supports and exit
  run-assistant --import-encyclopedia DIR
                                     import the encyclopedia DVD's data folder
"""

import argparse
import sys

from . import APP_NAME, __version__, gtkcompat  # noqa: F401  (pins GI versions)
from .hotkeys import action_names


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run-assistant", description=f"{APP_NAME} {__version__}")
    parser.add_argument("--action", choices=action_names() + ["settings", "diagnostics"],
                        help="perform an action in the running instance, then exit")
    parser.add_argument("--probe", action="store_true",
                        help="report display server, capture and hotkey support")
    parser.add_argument("--import-encyclopedia", metavar="DIR",
                        help="import the encyclopedia DVD's data "
                             "folder (the one holding ARTICLE_DATA.zip), then exit")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def probe():
    from . import session
    from .capture import CaptureManager

    print(session.summary())
    manager = CaptureManager()
    chain = manager.describe_chain()
    print(f"Capture chain  : {' -> '.join(chain) if chain else 'NONE - see README'}")
    from . import icon
    resolved = icon.name()
    print(f"Tray backend   : {gtkcompat.load_indicator()[1]}")
    print(f"Tray icon      : {resolved}"
          f"{'' if resolved == icon.NAME else ' (bundled icons not found)'}")
    print(f"WebKit view    : {gtkcompat.webkit_status()}")
    from . import bundle
    if bundle.status():
        print(f"Fallbacks      : {bundle.status()}")
    advice = gtkcompat.webkit_advice()
    if advice:
        print(advice)
    print(f"Notifications  : {'libnotify' if gtkcompat.has_notify() else 'stdout only'}")
    from . import theming
    for line in theming.summary():
        print(line)

    from .config import Config
    from .hotkeys import bindings, choose
    config = Config()
    print(f"Config file    : {config.config_file}")
    print(f"API key set    : {'yes' if config.api_key else 'NO - set one in Settings'}")
    print(f"Keyring        : {config.keychain_error or config.keychain.describe()}")
    from .app import privacy_lines
    for line in privacy_lines(config):
        print(line.replace(" : ", "  : ", 1))
    mechanism, why = choose(config)
    print(f"Shortcuts      : {mechanism} ({why}); setting {config.hotkey_mode}")
    print(f"  chosen       : {len(bindings(config))} - set them in Settings > Shortcuts")
    from . import errors, ipc
    print(f"Error log      : {errors.log_path()}")
    print(f"Running already: {'yes' if ipc.is_running() else 'no'}")


def import_encyclopedia(folder):
    """Import from the command line, and remember the folder for the app."""
    import os
    from .config import Config
    from .reference.axion import AxionError
    from .reference.encyclopedia import Encyclopedia, EncyclopediaError

    config = Config()
    encyclopedia = Encyclopedia(config)
    last = [None]

    def progress(fraction, message):
        # A line per percent: the import reports every few hundred articles.
        percent = f"{fraction:5.0%}"
        if percent != last[0]:
            print(f"{percent}  {message}", flush=True)
            last[0] = percent

    folder = os.path.abspath(os.path.expanduser(folder))
    try:
        meta = encyclopedia.import_from(folder, progress)
    except (AxionError, EncyclopediaError, OSError) as e:
        print(f"Import failed: {e}", file=sys.stderr)
        return 1
    config.encyclopedia_dir = folder
    config.save()
    print(f"Imported {meta['EB']} + {meta['EBI']} + {meta['EBK']} articles into "
          f"{encyclopedia.path}")
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.probe:
        probe()
        return 0

    if args.import_encyclopedia:
        return import_encyclopedia(args.import_encyclopedia)

    from . import ipc

    if args.action:
        ok, message = ipc.send_action(args.action)
        if ok:
            return 0
        print(f"Could not reach a running {APP_NAME}: {message}", file=sys.stderr)
        print("Start it first (run-assistant), then retry.", file=sys.stderr)
        return 1

    if ipc.is_running():
        print(f"{APP_NAME} is already running (check the system tray).", file=sys.stderr)
        return 1

    from .app import Assistant
    app = Assistant()
    if not app.is_primary:
        print(f"{APP_NAME} is already running.", file=sys.stderr)
        return 1
    try:
        app.run()
    except KeyboardInterrupt:
        app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
