"""Keys the app takes for itself in a text box, given to the input method first.

The compose boxes send on Enter, and the dialogs close on Escape, from key-press
handlers that run before the text view's own - which is where an input method sees the
key. Typing Chinese or Japanese, Enter commits the candidate being composed and Escape
abandons it: taken by the app instead, the message went without the text still being
composed, or the dialog closed under it.
"""

from .. import gtkcompat  # noqa: F401  (pins GI versions)
from gi.repository import Gdk, Gtk

ENTER = (Gdk.KEY_Return, Gdk.KEY_KP_Enter)


def input_method_takes(widget, event):
    """Whether `widget`'s input method used `event` - a candidate committed or
    abandoned - so that the app must not act on it as well. Asked only for keys the
    app is about to take (Enter, Escape): asked for every key, then let through to the
    view, which asks again, a key could reach the input method twice."""
    if not isinstance(widget, Gtk.TextView) or getattr(event, "window", None) is None:
        return False            # not a text box, or a key no window sent (made up)
    filter_keypress = getattr(widget, "im_context_filter_keypress", None)
    try:
        return bool(filter_keypress and filter_keypress(event))
    except TypeError:       # an event the binding will not pass on
        return False


def control_enter(event):
    """Ctrl+Enter: send, from wherever the focus is - the one way to send from the
    keyboard where Enter types a new line in the text being reviewed."""
    return (event.keyval in ENTER
            and bool(event.state & Gdk.ModifierType.CONTROL_MASK))
