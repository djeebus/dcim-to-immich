"""Configuration: the Immich server, the people who use this machine, and whose camera is whose.

A grown-up fills in "server", "users" and optionally "album" by hand; "cameras" is
filled in when someone taps their name the first time their camera is plugged in.

A user is either "Name": "<api key>", or "Name": {"key": "<api key>", "album": ...}
to give them a different album than everyone else. An album is a name or an ID:
"Camera uploads", {"name": "Camera uploads"} or {"id": "<uuid>"}.
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

USER_FIELDS = {"key", "album"}


class ConfigError(Exception):
    """The config file is broken. The message says where, for a grown-up to fix."""


def album_value(value: object, where: str = '"album"') -> str:
    """An album setting as the name or ID to look up; "" for none."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict) and len(value) == 1:
        (kind, v), = value.items()
        if kind in ("id", "name") and isinstance(v, str):
            return v.strip()
    raise ConfigError(
        f'{where} should be an album name or ID, like "Camera uploads" or {{"id": "<album id>"}}; '
        f"got {json.dumps(value)}"
    )


def _check(raw: object) -> None:
    """Raise ConfigError for anything the rest of the program would trip over."""
    if not isinstance(raw, dict):
        raise ConfigError("the file should be a JSON object: { ... }")
    if not isinstance(raw.get("server", ""), str):
        raise ConfigError('"server" should be the Immich address, like "https://photos.example.com"')
    album_value(raw.get("album"))
    users = raw.get("users", {})
    if not isinstance(users, dict):
        raise ConfigError('"users" should look like {"Bob": "<api key>", ...}')
    for name, entry in users.items():
        where = f'users → "{name}"'
        if isinstance(entry, str):
            continue
        if not isinstance(entry, dict):
            raise ConfigError(f'{where} should be an API key, or {{"key": "<api key>", "album": "..."}}')
        unknown = sorted(set(entry) - USER_FIELDS)
        if unknown:
            raise ConfigError(f"{where} has unknown setting(s) {', '.join(map(json.dumps, unknown))}; "
                              'the allowed ones are "key" and "album"')
        if not isinstance(entry.get("key"), str) or not entry["key"].strip():
            raise ConfigError(f'{where} needs its API key, as "key": "<api key>"')
        if "album" in entry:
            album_value(entry["album"], f"{where} → album")
    cameras = raw.get("cameras", {})
    if not isinstance(cameras, dict) or not all(isinstance(v, str) for v in cameras.values()):
        raise ConfigError('"cameras" should look like {"<camera id>": "<user name>", ...}')


@dataclass
class Config:
    path: Path = CONFIG_PATH
    server: str = ""
    users: dict[str, str | dict] = field(default_factory=dict)  # name -> API key, or {"key", "album"}
    album: str | dict = ""  # album every upload is added to (see album_value); "" for none
    cameras: dict[str, str] = field(default_factory=dict)  # camera id -> user name

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> Config:
        """Raises ConfigError, naming the file and the problem, if it's broken."""
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls(path=path)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path} isn't valid JSON: {e.msg} (line {e.lineno}, column {e.colno})") from e
        except OSError as e:
            raise ConfigError(f"Can't read {path}: {e.strerror}") from e
        try:
            _check(raw)
        except ConfigError as e:
            raise ConfigError(f"Problem in {path}: {e}") from None
        return cls(
            path=path,
            server=raw.get("server", ""),
            album=raw.get("album", ""),
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
            entry = self.users.get(user)
        return entry.get("key") if isinstance(entry, dict) else entry

    def album_for(self, user: str) -> str:
        with self._lock:
            entry = self.users.get(user)
            album = entry.get("album", self.album) if isinstance(entry, dict) else self.album
        return album_value(album)

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
        if self.album:
            data["album"] = self.album
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
