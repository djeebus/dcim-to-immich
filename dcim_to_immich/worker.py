"""The per-camera job: identify, upload every photo/video, delete what made it."""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import queue
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .camera import CameraError, Identity, MediaItem
from .config import CACHE_DIR, Config
from .immich import AuthError, ImmichClient, ImmichError

log = logging.getLogger(__name__)

# Give up on the rest of the camera after this many failures in a row.
MAX_CONSECUTIVE_FAILURES = 3


class Cancelled(Exception):
    pass


class Reassign(Exception):
    """The kid tapped "Not <name>?": stop, and ask whose camera it is again."""


class Kept(Exception):
    """A file deliberately left on the camera. Not a failure of the camera or server."""


@dataclass
class Summary:
    total: int = 0
    uploaded: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (file name, reason)
    stopped_reason: str | None = None
    cancelled: bool = False
    # Files uploaded to someone else before a "Not <name>?" switch: user -> count.
    earlier: dict[str, int] = field(default_factory=dict)


class Events(Protocol):
    """Callbacks from the worker thread. Implementations must be thread-safe."""

    def status(self, text: str) -> None: ...
    def choose_user(self, identity: Identity, users: list[str], error: str | None) -> None: ...
    def connected(self, identity: Identity, owner: str) -> None: ...
    def found(self, photos: int, videos: int) -> None: ...
    def progress(self, done: int, total: int, fraction: float, current: str) -> None: ...
    def finished(self, summary: Summary) -> None: ...
    def failed(self, message: str) -> None: ...


class Job(threading.Thread):
    def __init__(self, camera, config: Config, events: Events, client_factory=ImmichClient, cache_dir: Path = CACHE_DIR):
        super().__init__(daemon=True, name=f"job-{getattr(camera, 'port', '?')}")
        self.camera = camera
        self.config = config
        self.events = events
        self.client_factory = client_factory
        self.cache_dir = cache_dir
        self._cancel = threading.Event()
        self._reassign = threading.Event()
        self._answers: queue.Queue[str | None] = queue.Queue()
        self._earlier: dict[str, int] = {}

    # Called from the UI thread.
    def cancel(self) -> None:
        self._cancel.set()
        self._answers.put(None)

    def choose(self, user: str) -> None:
        self._answers.put(user)

    def reassign(self) -> None:
        self._reassign.set()

    def run(self) -> None:
        try:
            self._run()
        except Cancelled:
            self.camera.close()
            self.events.finished(Summary(cancelled=True, earlier=self._earlier))
        except (CameraError, ImmichError) as e:
            log.warning("job failed: %s", e)
            self.camera.close()
            self.events.failed(str(e))
        except Exception as e:
            log.exception("job crashed")
            self.camera.close()
            self.events.failed(f"Unexpected error: {e}")

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()
        if self._reassign.is_set():
            raise Reassign()

    def _run(self) -> None:
        self.events.status("Connecting to the camera…")
        self.camera.open()
        identity = self.camera.identity()
        log.info("camera %s (%s)", identity.camera_id, identity.manufacturer)

        user = self.config.owner(identity.camera_id)
        while True:
            self._reassign.clear()
            client, user = self._client_for(identity, user)
            summary = Summary(earlier=self._earlier)
            try:
                self._upload_all(client, identity, user, summary)
                break
            except Reassign:
                log.info("%s is not %s's camera; asking again", identity.camera_id, user)
                if summary.uploaded:
                    self._earlier[user] = self._earlier.get(user, 0) + summary.uploaded
                self.config.unassign(identity.camera_id)
                user = None
        self.camera.close()
        self.events.finished(summary)

    def _upload_all(self, client: ImmichClient, identity: Identity, user: str, summary: Summary) -> None:
        self.events.status("Looking for photos and videos…")
        items = self.camera.list_media()
        photos = sum(1 for i in items if i.kind == "photo")
        self.events.found(photos, len(items) - photos)

        summary.total = len(items)
        streak = 0
        for index, item in enumerate(items):
            self._check_cancel()
            self.events.progress(index, len(items), 0.0, item.name)
            try:
                self._transfer(client, identity, user, item, index, len(items))
                summary.uploaded += 1
                streak = 0
            except Cancelled:
                summary.cancelled = True
                break
            except Kept as e:
                log.info("%s: %s", item.path, e)
                summary.failed.append((item.name, str(e)))
            except (CameraError, ImmichError) as e:
                log.warning("%s: %s", item.path, e)
                summary.failed.append((item.name, str(e)))
                streak += 1
                if isinstance(e, AuthError) or streak >= MAX_CONSECUTIVE_FAILURES:
                    summary.stopped_reason = str(e)
                    break
            self.events.progress(index + 1, len(items), 0.0, "")

    def _client_for(self, identity: Identity, user: str | None) -> tuple[ImmichClient, str]:
        if not self.config.server or not self.config.users:
            raise ImmichError(f"No Immich server or users are set up yet. Ask a grown-up to edit {self.config.path}.")
        is_new = user is None
        error = None
        while True:
            if user is None:
                self.events.choose_user(identity, self.config.user_names(), error)
                user = self._answers.get()
                if self._cancel.is_set() or user is None:
                    raise Cancelled()
            client = self.client_factory(self.config.server, self.config.api_key(user) or "")
            try:
                client.whoami()
            except AuthError as e:
                message = f"{user}'s Immich key didn't work. Ask a grown-up to fix it."
                if not is_new:
                    raise AuthError(message, e.status) from e
                error, user = message, None
                continue
            if is_new:
                self.config.assign(identity.camera_id, user)
            self.events.connected(identity, user)
            return client, user

    def _transfer(self, client: ImmichClient, identity: Identity, user: str, item: MediaItem, index: int, total: int) -> None:
        """Copy one file to Immich, and delete it from the camera only once Immich has it.

        The one rule: camera.delete() runs only after the server, asked by checksum,
        says this user's library holds an asset with exactly the bytes we read off the
        camera, and that it isn't in the trash. Every other outcome, including any
        exception, leaves the file on the camera.
        """
        size = max(item.size, 1)

        def report(fraction: float) -> None:
            self._check_cancel()
            self.events.progress(index, total, fraction, item.name)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        ext = posixpath.splitext(item.name)[1]
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, suffix=ext)
        try:
            sha1 = hashlib.sha1()
            with os.fdopen(fd, "wb") as f:

                def write(chunk) -> None:
                    f.write(chunk)
                    sha1.update(chunk)

                # Raises unless exactly item.size bytes were read.
                self.camera.download(item, write, lambda n: report(0.5 * n / size))
            checksum = sha1.hexdigest()

            existing = client.find_existing(checksum)
            if existing is None:
                ts = datetime.fromtimestamp(item.mtime).astimezone() if item.mtime else datetime.now().astimezone()
                result = client.upload(
                    tmp,
                    filename=item.name,
                    device_asset_id=f"{item.name}-{item.size}".replace(" ", ""),
                    device_id=f"dcim-to-immich-{identity.serial}",
                    created_at=ts.isoformat(),
                    modified_at=ts.isoformat(),
                    progress=lambda n: report(0.5 + 0.5 * n / size),
                )
                log.info("%s uploaded as %s (%s)", item.path, result.asset_id, result.status)
                # Don't trust the upload response alone: ask again, by checksum.
                existing = client.find_existing(checksum)
                if existing is None:
                    raise ImmichError(f"Immich didn't confirm it has {item.name}, so it's staying on the camera.")
            else:
                log.info("%s already in Immich as %s", item.path, existing.asset_id)

            if existing.trashed:
                raise Kept(f"It's in {user}'s Immich trash, so it's staying on the camera.")
            self.camera.delete(item)
        finally:
            os.unlink(tmp)
