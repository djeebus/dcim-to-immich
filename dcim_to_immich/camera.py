"""Talking to a PTP camera through libgphoto2."""

from __future__ import annotations

import logging
import posixpath
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

PHOTO_EXTS = {".jpg", ".jpeg", ".cr2", ".cr3", ".crw", ".dng", ".png", ".heic", ".heif", ".tif", ".tiff"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts", ".3gp"}
CHUNK_SIZE = 1 << 20
# PTP's 32-bit size field saturates here for files of 4 GiB or more.
PTP_SIZE_UNKNOWN = 0xFFFFFFFF


class CameraError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    manufacturer: str
    model: str
    serial: str

    @property
    def camera_id(self) -> str:
        return f"{self.model}#{self.serial}"


@dataclass(frozen=True)
class MediaItem:
    folder: str
    name: str
    size: int
    mtime: int
    kind: str  # "photo" or "video"

    @property
    def path(self) -> str:
        return posixpath.join(self.folder, self.name)


def classify(name: str) -> str | None:
    ext = posixpath.splitext(name)[1].lower()
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def parse_summary(summary: str) -> dict[str, str]:
    """Pull 'Key: value' lines out of gphoto2's camera summary text."""
    fields = {}
    for line in summary.splitlines():
        m = re.match(r"\s*(Manufacturer|Model|Serial Number):\s*(.*?)\s*$", line)
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2)
    return fields


class GPhotoCamera:
    """One camera on one USB port. Not thread-safe: use from a single thread."""

    def __init__(self, port: str, usb_serial: str = ""):
        self.port = port
        self.usb_serial = usb_serial
        self._cam = None
        import gphoto2 as gp

        self._gp = gp

    def open(self, attempts: int = 5) -> None:
        gp = self._gp
        for attempt in range(1, attempts + 1):
            cam = gp.Camera()
            try:
                ports = gp.PortInfoList()
                ports.load()
                cam.set_port_info(ports[ports.lookup_path(self.port)])
                cam.init()
                self._cam = cam
                return
            except gp.GPhoto2Error as e:
                if attempt == attempts:
                    raise CameraError(f"Couldn't connect to the camera: {e}") from e
                if e.code == gp.GP_ERROR_IO_USB_CLAIM:
                    # GNOME's gvfs (or similar) grabbed the camera; ask it to let go.
                    log.info("camera on %s is busy, unmounting gphoto2 mounts", self.port)
                    subprocess.run(["gio", "mount", "-s", "gphoto2"], check=False, capture_output=True)
                else:
                    log.info("camera init on %s failed (%s), retrying", self.port, e)
                time.sleep(1.5)

    def identity(self) -> Identity:
        fields = parse_summary(str(self._call(self._cam.get_summary)))
        serial = fields.get("Serial Number", "").strip() or self.usb_serial
        if not serial:
            raise CameraError("This camera doesn't report a serial number, so it can't be told apart from others.")
        return Identity(
            manufacturer=fields.get("Manufacturer", ""),
            model=fields.get("Model", "Camera"),
            serial=serial,
        )

    def list_media(self) -> list[MediaItem]:
        """Everything under each storage's DCIM folder.

        Only DCIM: on a phone, images elsewhere (downloads, app data) aren't ours to
        upload and delete.
        """
        items: list[MediaItem] = []
        for dcim in self._find_dcim("/", depth=0):
            self._walk(dcim, items)
        items.sort(key=lambda i: (i.mtime, i.path))
        return items

    def _find_dcim(self, folder: str, depth: int) -> list[str]:
        # The layout is /<storage>/DCIM, e.g. /store_00010001/DCIM. Also accept /DCIM.
        found = []
        for sub, _ in self._call(self._cam.folder_list_folders, folder):
            path = posixpath.join(folder, sub)
            if sub.upper() == "DCIM":
                found.append(path)
            elif depth == 0:
                found.extend(self._find_dcim(path, depth + 1))
        return found

    def _walk(self, folder: str, out: list[MediaItem]) -> None:
        for name, _ in self._call(self._cam.folder_list_files, folder):
            kind = classify(name)
            if kind is None:
                continue
            info = self._call(self._cam.file_get_info, folder, name).file
            out.append(MediaItem(folder, name, int(info.size), int(info.mtime), kind))
        for sub, _ in self._call(self._cam.folder_list_folders, folder):
            self._walk(posixpath.join(folder, sub), out)

    def download(self, item: MediaItem, write: Callable[[bytes], object], progress: Callable[[int], None]) -> None:
        """Stream a file off the camera in chunks, falling back to a whole-file read.

        Raises unless exactly the camera's stated size was read, and that size is real.
        """
        if item.size <= 0 or item.size >= PTP_SIZE_UNKNOWN:
            raise CameraError(f"The camera didn't report a usable size for {item.name}; leaving it there.")
        gp = self._gp
        buf = bytearray(CHUNK_SIZE)
        view = memoryview(buf)
        offset = 0
        try:
            while offset < item.size:
                n = self._cam.file_read(item.folder, item.name, gp.GP_FILE_TYPE_NORMAL, offset, view)
                if n <= 0:
                    break
                write(view[:n])
                offset += n
                progress(offset)
        except gp.GPhoto2Error as e:
            if offset or e.code != gp.GP_ERROR_NOT_SUPPORTED:
                raise CameraError(f"Couldn't read {item.name} from the camera: {e}") from e
            data = self._call(self._cam.file_get, item.folder, item.name, gp.GP_FILE_TYPE_NORMAL)
            data = memoryview(data.get_data_and_size())
            write(data)
            offset = len(data)
            progress(offset)
        if offset != item.size:
            raise CameraError(f"Only got {offset} of {item.size} bytes of {item.name}")

    def delete(self, item: MediaItem) -> None:
        self._call(self._cam.file_delete, item.folder, item.name)

    def close(self) -> None:
        if self._cam is not None:
            cam, self._cam = self._cam, None
            try:
                cam.exit()
            except self._gp.GPhoto2Error as e:
                log.debug("camera exit: %s", e)

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except self._gp.GPhoto2Error as e:
            raise CameraError(str(e)) from e
