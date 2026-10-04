"""Single-instance D-Bus service.

The static-hotkey path needs a way for a freshly spawned `run-assistant --action=ocr_only`
to reach the already-running tray app instead of starting a second copy. Owning a
well-known bus name gives us both the uniqueness check and the message channel.

This is what makes hotkeys work on Wayland at all: the desktop's own keyboard settings
invoke a short-lived process, which hands the action to the running instance and exits.
"""

from gi.repository import Gio, GLib

from . import APP_ID

OBJECT_PATH = "/org/digitalarchon/DAAssistant"
INTERFACE = "org.digitalarchon.DAAssistant"

INTROSPECTION = f"""
<node>
  <interface name='{INTERFACE}'>
    <method name='Activate'>
      <arg type='s' name='action' direction='in'/>
      <arg type='s' name='result' direction='out'/>
    </method>
    <method name='Ping'>
      <arg type='s' name='result' direction='out'/>
    </method>
  </interface>
</node>
"""


class Service:
    """Owns the bus name and dispatches incoming actions."""

    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.bus = None
        self.owner_id = None
        self.registration_id = None

    def acquire(self):
        """Try to become the primary instance. Returns True if we own the name."""
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)

        try:
            self.registration_id = self.bus.register_object(
                OBJECT_PATH, node.interfaces[0], self._on_call, None, None)
        except GLib.Error:
            return False

        flags = Gio.BusNameOwnerFlags.DO_NOT_QUEUE
        result = self.bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "RequestName", GLib.Variant("(su)", (APP_ID, flags)),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None)
        # 1 = PRIMARY_OWNER, 4 = ALREADY_OWNER
        code = result.unpack()[0]
        owned = code in (1, 4)
        if not owned and self.registration_id is not None:
            self.bus.unregister_object(self.registration_id)
            self.registration_id = None
        return owned

    def _on_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "Ping":
            invocation.return_value(GLib.Variant("(s)", ("pong",)))
            return
        if method == "Activate":
            action = params.unpack()[0]
            GLib.idle_add(self.dispatch, action)
            invocation.return_value(GLib.Variant("(s)", ("ok",)))
            return
        invocation.return_error_literal(
            Gio.DBusError.quark(), Gio.DBusError.UNKNOWN_METHOD, f"Unknown: {method}")

    def release(self):
        if self.bus is not None and self.registration_id is not None:
            self.bus.unregister_object(self.registration_id)
            self.registration_id = None


def send_action(action, timeout_ms=5000):
    """Ask a running instance to perform `action`. Returns (ok, message)."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            APP_ID, OBJECT_PATH, INTERFACE, "Activate",
            GLib.Variant("(s)", (action,)), GLib.VariantType("(s)"),
            Gio.DBusCallFlags.NONE, timeout_ms, None)
    except GLib.Error as e:
        return False, e.message
    return True, "ok"


def is_running():
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(APP_ID, OBJECT_PATH, INTERFACE, "Ping",
                      None, GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error:
        return False
    return True
