# DA Assistant

A system-tray assistant for Linux that translates and answers questions about whatever is
on your screen or in your clipboard, using any OpenAI-compatible chat API.

> [!WARNING]
> **DA Assistant is beta software.** Expect rough edges, and settings that may change
> between versions. Everything you capture or copy into it is sent to the API provider
> you configure: check what is in a screenshot before you send it, and read
> [Privacy Mode](MANUAL.md#privacy-mode) if that matters to you. Answers are written by
> language models and can be wrong, even when they sound sure.

## What it does

Eight operations, from the tray menu or a global shortcut you choose:

| Action | Suggested key | What it does |
|---|---|---|
| Translate clipboard text | `Ctrl+Shift+1` | Translates the clipboard into your language |
| Translate image selection | `Ctrl+Shift+2` | Reads the text in a screen region, then translates it |
| Query clipboard text | `Ctrl+Shift+3` | Ask a question about the clipboard |
| Query image selection | `Ctrl+Shift+4` | Ask a question about a screen region |
| OCR only | `Ctrl+Shift+5` | Reads the text in a screen region, word for word |
| Look up word | `Ctrl+Shift+6` | Every entry your own dictionaries (DSL, StarDict) have for a word |
| Search Encyclopedia | `Ctrl+Shift+7` | Read your encyclopedia DVD - [off by default](#the-encyclopedia) |
| Ask Encyclopedia | `Ctrl+Shift+8` | Answers from the encyclopedia alone, every quotation checked - [off by default](#the-encyclopedia) |

**Ask anything**, at the top of the tray menu, opens a chat with nothing captured, to
type into straight away (suggested key `Ctrl+Shift+9`).

No shortcut is set until you choose one - see [Shortcuts](#shortcuts).

Answers open in a chat window that streams as it writes, renders Markdown, and takes
follow-up questions. Any chat can take attached pictures and text files (Markdown, JSON,
logs, code). Pictures, and screen regions you ask about, can have anything sensitive
blacked out first, and are re-encoded from their pixels alone, so no metadata goes with
them. You choose which model does each job, and can switch models
mid-conversation. Web search is optional, per request. **Privacy Mode** sends everything
only to models running in an attested trusted execution environment, proving each one
before anything is sent.

The [manual](MANUAL.md) covers all of it in detail.

## Installing

### The AppImage

The simplest way: one file, with its own Python and everything else it needs from Python.

```bash
chmod +x DA_Assistant-*-x86_64.AppImage
./DA_Assistant-*-x86_64.AppImage --probe     # what it found on this machine
./DA_Assistant-*-x86_64.AppImage             # start it: look for the icon in the tray
```

It needs a system with GLib 2.80 or newer: Ubuntu 24.04 / Linux Mint 22, Debian 13, or
current Fedora, Arch and CachyOS. GTK 3 and WebKitGTK come from the system, so they get
its security updates. Install WebKitGTK for the full chat view (without it, answers show
as plain text):

| Distribution | Install |
|---|---|
| Ubuntu, Mint, Debian | `sudo apt install gir1.2-webkit2-4.1` |
| Fedora | `sudo dnf install webkit2gtk4.1` |
| Arch, CachyOS | `sudo pacman -S webkit2gtk-4.1` |

Every release can be rebuilt from its source and checked byte for byte against the one
published; [packaging/README.md](packaging/README.md) says how.

### From source

Python 3.10+, GTK 3, and:

**Debian / Ubuntu / Linux Mint**
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
  gir1.2-webkit2-4.1 gir1.2-notify-0.7 gir1.2-ayatanaappindicator3-0.1 \
  python3-pil python3-requests python3-markdown python3-pygments python3-xlib \
  python3-cryptography python3-keyring
```

**Arch / CachyOS**
```bash
sudo pacman -S python-gobject gtk3 webkit2gtk-4.1 libnotify \
  libayatana-appindicator python-pillow python-requests \
  python-markdown python-pygments python-xlib python-cryptography python-keyring
```

**Fedora**
```bash
sudo dnf install python3-gobject gtk3 webkit2gtk4.1 libnotify \
  libayatana-appindicator-gtk3 python3-pillow python3-requests \
  python3-markdown python3-pygments python3-xlib python3-cryptography python3-keyring
```

Then:

```bash
./run-assistant --probe     # what it found on this machine
./run-assistant             # start it
```

On Wayland, install `wl-clipboard` (KDE needs nothing: it uses Klipper), and on KDE,
`spectacle` for selecting screen regions. The manual has the details:
[What it uses from the system](MANUAL.md#what-it-uses-from-the-system).

## First run

1. Open **Settings** from the tray icon.
2. Under **General**, enter your provider's **API URL** and **API key**, and choose the
   models: **Browse…** lists everything the provider offers, with prices.
3. Press **Test connection**: it shows the provider's own error message if anything is
   wrong.

Your settings are kept in `~/.config/da-assistant/config.json`. The API key is not: it
goes into your desktop's keyring (GNOME Keyring, or KWallet on Plasma), which encrypts
it. Without a keyring, a key typed into Settings works until the app quits, and Settings
says why it cannot be kept.

## Shortcuts

Open **Settings > Shortcuts**, click an action's button and press the keys you want, or
press **Use suggested keys** for `Ctrl+Shift+1` to `9`. Nothing is bound until you do.
How they are bound depends on your session, and the page says which:

- **X11** (Cinnamon, MATE, XFCE, or KDE and GNOME on Xorg): the app listens for the keys
  itself, and tells you if another program already has one.
- **KDE Plasma 6, GNOME 48 and later** on Wayland: your desktop binds them, through the
  GlobalShortcuts portal, and may ask you to confirm them. Change them later in its own
  shortcut settings.
- **GNOME before 48, Cinnamon, Hyprland and Sway** on Wayland: the app writes them into
  the desktop's own keybindings when you press Save, and leaves everything else there as
  it was.
- **Anything else**: the page lists the commands to bind by hand.

See [Global shortcuts](MANUAL.md#global-shortcuts) for more.

## The encyclopedia

If you have a purchased copy of the **Britannica Ultimate Reference Suite 2015** DVD,
copy its `data` folder to your computer and turn on **Settings > Reference > Search
Encyclopedia and Ask Encyclopedia**. Both are off by default: until then they are not in
the tray menu, and no shortcut is bound to them. Earlier editions of
the DVD may work too, but only the 2015 edition has been tested.

DA Assistant is not affiliated with Britannica in any way. The purpose of these two
windows is simply to make it easy to read your own DVD on Linux, without needing Bottles
or a virtual machine to run the program it came with. None of the DVD's content is part
of this app; it is read from your copy. Setting it up is described under
[Search Encyclopedia and Ask Encyclopedia](MANUAL.md#search-encyclopedia-and-ask-encyclopedia).

## Help

Run `--probe`, or open **Diagnostics** from the tray menu (it has a Copy button), to see
what the app found. The manual's [Troubleshooting](MANUAL.md#troubleshooting) covers the
common problems.

## License

DA Assistant is free software: you can redistribute it and modify it under the terms of
the GNU General Public License, version 3 or (at your option) any later version. See
[LICENSE](LICENSE). It comes with no warranty.
