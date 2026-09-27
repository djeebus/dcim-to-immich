"""Watch udev for cameras coming and going; one window + job per camera."""

from __future__ import annotations

import logging

import pyudev
from gi.repository import GLib, Gtk

from .camera import GPhotoCamera
from .config import Config
from .ui import CameraWindow, GtkEvents
from .worker import Job

log = logging.getLogger(__name__)


def is_camera(device: pyudev.Device) -> bool:
    props = device.properties
    # Set by udev/72-dcim-to-immich.rules. The rest is a fallback for when the rule
    # isn't installed (e.g. development), though then the desktop may grab the device.
    return (
        props.get("DCIM_TO_IMMICH") == "1"
        or ":060101:" in props.get("ID_USB_INTERFACES", "")
        or props.get("ID_MTP_DEVICE") == "1"
    )


class App:
    def __init__(self, config: Config):
        self.config = config
        self.windows: dict[str, CameraWindow] = {}
        self.context = pyudev.Context()
        self.monitor = pyudev.Monitor.from_netlink(self.context)
        self.monitor.filter_by("usb", "usb_device")

    def run(self) -> None:
        self.monitor.start()
        GLib.io_add_watch(self.monitor.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._on_uevent)
        for device in self.context.list_devices(subsystem="usb", DEVTYPE="usb_device"):
            self._add(device)
        log.info("watching for PTP/MTP devices")
        Gtk.main()

    def _on_uevent(self, *_args) -> bool:
        while (device := self.monitor.poll(timeout=0)) is not None:
            if device.action in ("add", "bind"):
                self._add(device)
            elif device.action == "remove":
                self._remove(device)
        return True

    def _add(self, device: pyudev.Device) -> None:
        if device.sys_path in self.windows or not is_camera(device):
            return
        props = device.properties
        try:
            port = f"usb:{int(props['BUSNUM']):03d},{int(props['DEVNUM']):03d}"
        except (KeyError, ValueError):
            log.warning("no bus/device number for %s", device.sys_path)
            return
        log.info("camera plugged in: %s at %s", props.get("ID_MODEL", "?"), port)
        # Reload so cameras registered by another window are picked up.
        self.config = Config.load(self.config.path)
        window = CameraWindow(self.config)
        camera = GPhotoCamera(port, usb_serial=props.get("ID_SERIAL_SHORT", ""))
        window.job = Job(camera, self.config, GtkEvents(window))
        window.connect("destroy", lambda *_: self.windows.pop(device.sys_path, None))
        self.windows[device.sys_path] = window
        window.job.start()

    def _remove(self, device: pyudev.Device) -> None:
        window = self.windows.pop(device.sys_path, None)
        if window is not None:
            log.info("camera removed: %s", device.sys_path)
            if window.job is not None:
                window.job.cancel()
            window.destroy()
