"""The per-camera window, sized for fingers on a touchscreen (GTK 3)."""

from __future__ import annotations

from gi.repository import Gdk, GLib, Gtk, Pango

from .camera import Identity
from .config import Config
from .worker import Job, Summary

# Big text and big targets: this runs on a touchscreen, mostly used by kids.
CSS = b"""
window.kiosk { font-size: 16pt; }
.headline { font-size: 24pt; font-weight: bold; }
.big { font-size: 19pt; }
.dim { opacity: 0.7; }
.err { color: #e01b24; font-weight: bold; }
button.touch { min-height: 72px; min-width: 180px; padding: 8px 28px; font-size: 18pt; }
button.user { min-height: 110px; min-width: 220px; font-size: 24pt; font-weight: bold; }
progressbar trough, progressbar progress { min-height: 36px; border-radius: 18px; }
progressbar text { font-size: 17pt; }
"""

ICON_PX = 96

_css_loaded = False


def _load_css() -> None:
    global _css_loaded
    if not _css_loaded:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _css_loaded = True


def _label(text: str = "", *classes: str, wrap: bool = True) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=0)
    lbl.set_line_wrap(wrap)
    lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lbl.set_max_width_chars(40)
    for c in classes:
        lbl.get_style_context().add_class(c)
    return lbl


def _button(text: str, *classes: str) -> Gtk.Button:
    btn = Gtk.Button(label=text)
    for c in ("touch", *classes):
        btn.get_style_context().add_class(c)
    return btn


def _icon(name: str) -> Gtk.Image:
    img = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.DIALOG)
    img.set_pixel_size(ICON_PX)
    return img


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class CameraWindow(Gtk.Window):
    """Shows one camera's upload. Every public method must be called on the GTK thread."""

    def __init__(self, config: Config):
        super().__init__(title="Camera upload")
        _load_css()
        self.config = config
        self.job: Job | None = None
        self.running = True
        self.stopping = False
        self.closed = False
        self.get_style_context().add_class("kiosk")
        self.set_icon_name("camera-photo")
        self.set_default_size(760, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.connect("delete-event", self._on_delete)
        self.connect("destroy", lambda *_: setattr(self, "closed", True))

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28)
        outer.set_border_width(36)
        self.add(outer)

        header = Gtk.Box(spacing=20)
        header.pack_start(_icon("camera-photo"), False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        self.title_lbl = _label("Camera", "headline")
        self.subtitle_lbl = _label("", "dim")
        titles.pack_start(self.title_lbl, False, False, 0)
        titles.pack_start(self.subtitle_lbl, False, False, 0)
        header.pack_start(titles, True, True, 0)
        outer.pack_start(header, False, False, 0)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False, hhomogeneous=True)
        outer.pack_start(self.stack, True, True, 0)
        self.stack.add_named(self._build_busy(), "busy")
        self.stack.add_named(self._build_choose(), "choose")
        self.stack.add_named(self._build_progress(), "progress")
        self.stack.add_named(self._build_done(), "done")

        self.buttons = Gtk.Box(spacing=16)
        # Tapped the wrong name? This stops and asks again.
        self.not_user_btn = _button("")
        self.not_user_btn.set_no_show_all(True)
        self.not_user_btn.connect("clicked", self._on_not_user)
        self.buttons.pack_start(self.not_user_btn, False, False, 0)
        self.close_btn = _button("Stop")
        self.close_btn.connect("clicked", lambda *_: self._on_delete())
        self.buttons.pack_end(self.close_btn, False, False, 0)
        outer.pack_start(self.buttons, False, False, 0)

        self.show_all()
        self.stack.set_visible_child_name("busy")

    # -- pages --------------------------------------------------------------

    def _build_busy(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=20)
        spinner = Gtk.Spinner(width_request=48, height_request=48)
        spinner.start()
        box.pack_start(spinner, False, False, 0)
        self.busy_lbl = _label("Connecting to the camera…", "big")
        box.pack_start(self.busy_lbl, True, True, 0)
        return box

    def _build_choose(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.pack_start(_label("Whose camera is this?", "big"), False, False, 0)
        self.choose_error = _label("", "err")
        box.pack_start(self.choose_error, False, False, 0)
        self.user_grid = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            min_children_per_line=2,
            max_children_per_line=3,
            row_spacing=16,
            column_spacing=16,
        )
        # Lots of users scroll (with a finger) rather than growing off the screen.
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_propagate_natural_height(True)
        scroll.set_max_content_height(420)
        scroll.set_kinetic_scrolling(True)
        scroll.add(self.user_grid)
        box.pack_start(scroll, True, True, 0)
        return box

    def _build_progress(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.found_lbl = _label("", "big")
        box.pack_start(self.found_lbl, False, False, 0)
        self.bar = Gtk.ProgressBar(show_text=True)
        box.pack_start(self.bar, False, False, 0)
        self.current_lbl = _label("", "dim", wrap=False)
        self.current_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        box.pack_start(self.current_lbl, False, False, 0)
        box.pack_start(_label("Don't unplug the camera yet.", "big"), False, False, 0)
        return box

    def _build_done(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        row = Gtk.Box(spacing=20)
        self.done_icon = _icon("emblem-ok-symbolic")
        row.pack_start(self.done_icon, False, False, 0)
        self.done_lbl = _label("", "headline")
        row.pack_start(self.done_lbl, True, True, 0)
        box.pack_start(row, False, False, 0)
        self.done_detail = _label("", "big")
        box.pack_start(self.done_detail, False, False, 0)
        self.done_errors = _label("", "dim")
        box.pack_start(self.done_errors, False, False, 0)
        return box

    # -- worker events (already marshalled onto the GTK thread) ------------

    def on_status(self, text: str) -> None:
        self.busy_lbl.set_text(text)
        self.stack.set_visible_child_name("busy")
        self._set_close("Stop")

    def on_choose_user(self, identity: Identity, users: list[str], error: str | None) -> None:
        self.title_lbl.set_text(identity.model)
        self.subtitle_lbl.set_text("")
        self.choose_error.set_text(error or "")
        self.choose_error.set_visible(bool(error))
        for child in self.user_grid.get_children():
            child.destroy()
        for name in users:
            btn = _button(name, "user")
            btn.connect("clicked", self._on_user, name)
            self.user_grid.add(btn)
        self.user_grid.show_all()
        self.not_user_btn.hide()
        self.stack.set_visible_child_name("choose")
        self._set_close("Cancel")
        self.present()

    def on_connected(self, identity: Identity, user: str) -> None:
        self.title_lbl.set_text(identity.model)
        self.subtitle_lbl.set_text(f"{user}'s camera")
        if self.running and not self.stopping:
            self.not_user_btn.set_label(f"Not {user}?")
            self.not_user_btn.show()

    def on_found(self, photos: int, videos: int) -> None:
        self.found_lbl.set_text(f"{_plural(photos, 'photo')} and {_plural(videos, 'video')} on the camera")
        self.bar.set_fraction(0)
        self.bar.set_text(f"0 of {photos + videos} uploaded")
        self.stack.set_visible_child_name("progress")
        self._set_close("Stop")

    def on_progress(self, done: int, total: int, fraction: float, current: str) -> None:
        if total:
            self.bar.set_fraction(min(1.0, (done + fraction) / total))
        self.bar.set_text(f"{done} of {total} uploaded")
        if current:
            verb = "Copying" if fraction < 0.5 else "Uploading"
            self.current_lbl.set_text(f"{verb} {current}…")
        else:
            self.current_lbl.set_text("")

    def on_finished(self, s: Summary) -> None:
        self._finish()
        errors = "\n".join(f"{name}: {why}" for name, why in s.failed[:10])
        earlier = "".join(f"\n{_plural(n, 'file')} went to {who} before switching." for who, n in s.earlier.items())
        if s.cancelled and not s.total:
            self._show_done("dialog-information", (earlier.strip() or "Nothing was changed on the camera."))
            return
        if s.total == 0:
            self._show_done("emblem-ok-symbolic", "There are no photos or videos on the camera." + earlier)
        elif not s.failed and not s.cancelled:
            self._show_done("emblem-ok-symbolic", f"All done! {_plural(s.uploaded, 'file')} uploaded." + earlier)
        else:
            left = s.total - s.uploaded
            detail = f"{s.uploaded} of {s.total} uploaded. {_plural(left, 'file')} stayed on the camera"
            detail += "." if s.cancelled else "; plug it in again to try again."
            detail += earlier
            if s.stopped_reason:
                detail += f"\n\n{s.stopped_reason}"
            self._show_done("dialog-warning", detail, errors)

    def on_failed(self, message: str) -> None:
        self._finish()
        self._show_done("dialog-error", f"{message}\n\nNothing was deleted.")

    # -- internals ----------------------------------------------------------

    def _show_done(self, icon: str, detail: str, errors: str = "") -> None:
        self.done_icon.set_from_icon_name(icon, Gtk.IconSize.DIALOG)
        self.done_icon.set_pixel_size(ICON_PX)
        self.done_lbl.set_text("It's safe to unplug the camera")
        self.done_detail.set_text(detail)
        self.done_errors.set_text(errors)
        self.done_errors.set_visible(bool(errors))
        self.stack.set_visible_child_name("done")
        self.present()

    def _set_close(self, label: str) -> None:
        if self.running and not self.stopping:
            self.close_btn.set_label(label)
            self.close_btn.set_sensitive(True)

    def _finish(self) -> None:
        self.running = False
        self.not_user_btn.hide()
        self.close_btn.set_label("Close")
        self.close_btn.set_sensitive(True)

    def _on_user(self, _btn, name: str) -> None:
        if self.job is None:
            return
        self.on_status(f"Getting {name}'s photos ready…")
        self.job.choose(name)

    def _on_not_user(self, *_args) -> None:
        if self.job is None or not self.running:
            return
        self.not_user_btn.hide()
        self.on_status("OK! Let's pick again…")
        self.job.reassign()

    def _on_delete(self, *_args) -> bool:
        if self.running and self.job is not None:
            # Let the job stop cleanly so we can say when it's safe to unplug.
            self.job.cancel()
            self.stopping = True
            self.not_user_btn.hide()
            self.close_btn.set_label("Stopping…")
            self.close_btn.set_sensitive(False)
            return True
        self.destroy()
        return True


class GtkEvents:
    """Adapts worker-thread callbacks onto the GTK main loop."""

    def __init__(self, window: CameraWindow):
        self.w = window

    def _post(self, fn, *args) -> None:
        def call():
            if not self.w.closed:
                fn(*args)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(call)

    def status(self, text):
        self._post(self.w.on_status, text)

    def choose_user(self, identity, users, error):
        self._post(self.w.on_choose_user, identity, users, error)

    def connected(self, identity, user):
        self._post(self.w.on_connected, identity, user)

    def found(self, photos, videos):
        self._post(self.w.on_found, photos, videos)

    def progress(self, done, total, fraction, current):
        self._post(self.w.on_progress, done, total, fraction, current)

    def finished(self, summary):
        self._post(self.w.on_finished, summary)

    def failed(self, message):
        self._post(self.w.on_failed, message)
