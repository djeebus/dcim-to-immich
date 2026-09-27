"""Stand-ins for a camera and an Immich server, for --demo and the tests."""

from __future__ import annotations

import hashlib
import time
import uuid

from .camera import CameraError, Identity, MediaItem
from .immich import AuthError, ExistingAsset, UploadResult


class FakeCamera:
    port = "fake"

    def __init__(self, files: dict[str, bytes], serial: str = "0123456789abcdef", delay: float = 0.0):
        self.files = dict(files)
        self.serial = serial
        self.delay = delay
        self.opened = False
        self.fail_download: set[str] = set()
        self.deleted: list[str] = []
        self.before_delete = lambda item, data: None  # tests assert invariants here

    def open(self):
        time.sleep(self.delay * 5)
        self.opened = True

    def identity(self):
        return Identity("Canon Inc.", "Canon PowerShot SX20 IS", self.serial)

    def list_media(self):
        time.sleep(self.delay * 5)
        return [
            MediaItem("/store_00010001/DCIM/100CANON", path.rsplit("/", 1)[-1], len(data), 1_700_000_000 + i,
                      "video" if path.lower().endswith(".mov") else "photo")
            for i, (path, data) in enumerate(sorted(self.files.items()))
        ]

    def download(self, item, write, progress):
        if item.name in self.fail_download:
            raise CameraError(f"I/O error reading {item.name}")
        data = self.files[item.name]
        step = max(1, len(data) // 10)
        for off in range(0, len(data), step):
            time.sleep(self.delay)
            write(data[off:off + step])
            progress(min(len(data), off + step))

    def delete(self, item):
        self.before_delete(item, self.files[item.name])
        del self.files[item.name]
        self.deleted.append(item.name)

    def close(self):
        self.opened = False


class FakeServer:
    """An Immich server: one library per API key."""

    def __init__(self, delay: float = 0.0, bad_keys=("broken-key",)):
        self.delay = delay
        self.bad_keys = set(bad_keys)
        self.libraries: dict[str, dict[str, ExistingAsset]] = {}  # key -> sha1 -> asset
        self.uploads: list[tuple[str, str]] = []  # (key, filename)
        self.lose_uploads = False  # say "created" but don't keep the file

    def library(self, key: str) -> dict[str, ExistingAsset]:
        return self.libraries.setdefault(key, {})

    def client(self, server: str, api_key: str) -> FakeImmich:
        return FakeImmich(self, api_key)


class FakeImmich:
    def __init__(self, server: FakeServer, api_key: str):
        self.srv, self.api_key = server, api_key

    def whoami(self):
        time.sleep(self.srv.delay * 5)
        if not self.api_key or self.api_key in self.srv.bad_keys:
            raise AuthError("The Immich API key was rejected.", 401)
        return "Demo User"

    def find_existing(self, sha1_hex):
        return self.srv.library(self.api_key).get(sha1_hex)

    def upload(self, path, *, filename, progress=lambda n: None, **_):
        with open(path, "rb") as f:
            data = f.read()
        for i in range(1, 11):
            time.sleep(self.srv.delay)
            progress(len(data) * i // 10)
        asset_id = str(uuid.uuid4())
        if not self.srv.lose_uploads:
            self.srv.library(self.api_key)[hashlib.sha1(data).hexdigest()] = ExistingAsset(asset_id, False)
        self.srv.uploads.append((self.api_key, filename))
        return UploadResult(asset_id, "created")
