"""Configuration: the Immich server, the people who use this machine, and whose camera is whose.

A grown-up fills in "server" and "users" by hand; "cameras" is filled in when someone
taps their name the first time their camera is plugged in.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default) / "dcim-to-immich"


CONFIG_PATH = _xdg("XDG_CONFIG_HOME", ".config") / "config.json"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache")


@dataclass
class Config:
    path: Path = CONFIG_PATH
    server: str = ""
    users: dict[str, str] = field(default_factory=dict)  # name -> Immich API key
    cameras: dict[str, str] = field(default_factory=dict)  # camera id -> user name

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> Config:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls(path=path)
        return cls(
            path=path,
            server=raw.get("server", ""),
            users=dict(raw.get("users", {})),
            cameras=dict(raw.get("cameras", {})),
        )

    def owner(self, camera_id: str) -> str | None:
        """The camera's user, if it has one who still exists."""
        with self._lock:
            name = self.cameras.get(camera_id)
            return name if name in self.users else None

    def api_key(self, user: str) -> str | None:
        with self._lock:
            return self.users.get(user)

    def user_names(self) -> list[str]:
        with self._lock:
            return sorted(self.users, key=str.casefold)

    def assign(self, camera_id: str, user: str) -> None:
        with self._lock:
            self.cameras[camera_id] = user
            self._save()

    def unassign(self, camera_id: str) -> None:
        with self._lock:
            if self.cameras.pop(camera_id, None) is not None:
                self._save()

    def _save(self) -> None:
        data = {"server": self.server, "users": self.users, "cameras": self.cameras}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The file holds API keys: write it private, and atomically.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise
