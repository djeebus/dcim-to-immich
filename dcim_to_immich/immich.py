"""Minimal Immich API client (stdlib only) with streaming, progress-reporting uploads."""

from __future__ import annotations

import http.client
import json
import mimetypes
import os
import re
import ssl
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

CHUNK_SIZE = 1 << 20


class ImmichError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthError(ImmichError):
    pass


class UnavailableError(ImmichError):
    pass


@dataclass
class UploadResult:
    asset_id: str
    status: str  # "created", "duplicate", ...


@dataclass
class ExistingAsset:
    asset_id: str
    trashed: bool


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def resolve_album(client, name_or_id: str) -> str:
    """Album ID for a configured album: an existing album's ID, or a name.

    A name that doesn't exist yet is created, so every user can share one setting
    (albums belong to each user) and get their own album.
    """
    albums = client.albums()
    for a in albums:
        if a.get("id") == name_or_id:
            return a["id"]
    if UUID_RE.fullmatch(name_or_id):
        raise ImmichError(f"There's no album with ID {name_or_id} (or this key can't see it).")
    for a in albums:
        if a.get("albumName") == name_or_id:
            return a["id"]
    return client.create_album(name_or_id)


def normalize_server(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    if url.endswith("/api"):
        url = url[: -len("/api")]
    return url


class ImmichClient:
    def __init__(self, server: str, api_key: str, timeout: float = 60):
        self.server = normalize_server(server)
        self.api_key = api_key.strip()
        self.timeout = timeout
        parts = urlsplit(self.server)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ImmichError(f"Not a valid server address: {server!r}")
        self._parts = parts
        self._prefix = parts.path.rstrip("/") + "/api"

    # -- plumbing -----------------------------------------------------------

    def _connect(self) -> http.client.HTTPConnection:
        p = self._parts
        if p.scheme == "https":
            return http.client.HTTPSConnection(p.hostname, p.port, timeout=self.timeout, context=ssl.create_default_context())
        return http.client.HTTPConnection(p.hostname, p.port, timeout=self.timeout)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"x-api-key": self.api_key, "Accept": "application/json", "User-Agent": "dcim-to-immich"}
        h.update(extra or {})
        return h

    def _finish(self, conn: http.client.HTTPConnection) -> tuple[int, object]:
        resp = conn.getresponse()
        raw = resp.read()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode(errors="replace")
        if resp.status == 401:
            raise AuthError("The Immich API key was rejected.", resp.status)
        if resp.status >= 400 and resp.status != 403:
            msg = body.get("message") if isinstance(body, dict) else body
            if isinstance(msg, list):
                msg = "; ".join(map(str, msg))
            raise ImmichError(f"Immich returned {resp.status}: {msg or resp.reason}", resp.status)
        return resp.status, body

    def _request(self, method: str, path: str, payload: object = None) -> tuple[int, object]:
        conn = self._connect()
        try:
            body = None if payload is None else json.dumps(payload).encode()
            headers = self._headers({"Content-Type": "application/json"} if body else None)
            conn.request(method, self._prefix + path, body=body, headers=headers)
            return self._finish(conn)
        except (OSError, http.client.HTTPException) as e:
            raise UnavailableError(f"Couldn't reach {self.server}: {e}") from e
        finally:
            conn.close()

    # -- API ----------------------------------------------------------------

    def whoami(self) -> str | None:
        """Validate the key. Returns the user's name, or None if the key can't read it."""
        status, body = self._request("GET", "/users/me")
        if status == 403:
            return None
        return body.get("name") or body.get("email")

    def find_existing(self, sha1_hex: str) -> ExistingAsset | None:
        """Look up an asset with exactly this SHA-1 in this key's user's library.

        This is what decides whether a file may be deleted from the camera, so anything
        other than a clear "yes, it's here" answer comes back as None.
        """
        try:
            status, body = self._request(
                "POST", "/assets/bulk-upload-check", {"assets": [{"id": "1", "checksum": sha1_hex}]}
            )
        except ImmichError as e:
            if e.status == 404:  # server too old to ask
                return None
            raise
        if status == 403 or not isinstance(body, dict):
            return None
        for r in body.get("results", []):
            if r.get("id") == "1" and r.get("action") == "reject" and r.get("reason") == "duplicate" and r.get("assetId"):
                return ExistingAsset(asset_id=r["assetId"], trashed=bool(r.get("isTrashed")))
        return None

    def upload(
        self,
        path: str,
        *,
        filename: str,
        device_asset_id: str,
        device_id: str,
        created_at: str,
        modified_at: str,
        progress: Callable[[int], None] = lambda n: None,
    ) -> UploadResult:
        size = os.path.getsize(path)
        boundary = uuid.uuid4().hex
        fields = {
            "deviceAssetId": device_asset_id,
            "deviceId": device_id,
            "fileCreatedAt": created_at,
            "fileModifiedAt": modified_at,
        }
        pre = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()
        )
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        pre += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="assetData"; filename="{safe_name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        post = f"\r\n--{boundary}--\r\n".encode()

        conn = self._connect()
        try:
            conn.putrequest("POST", self._prefix + "/assets")
            headers = self._headers(
                {
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(pre) + size + len(post)),
                }
            )
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.endheaders()
            conn.send(pre)
            sent = 0
            with open(path, "rb") as f:
                while chunk := f.read(CHUNK_SIZE):
                    conn.send(chunk)
                    sent += len(chunk)
                    progress(sent)
            conn.send(post)
            status, body = self._finish(conn)
        except (OSError, http.client.HTTPException) as e:
            raise UnavailableError(f"Upload to {self.server} failed: {e}") from e
        finally:
            conn.close()
        if status == 403:
            raise ImmichError("This API key isn't allowed to upload (it needs the asset.upload permission).", status)
        if not isinstance(body, dict) or not body.get("id"):
            raise ImmichError(f"Unexpected upload response: {body!r}", status)
        return UploadResult(asset_id=body["id"], status=body.get("status", "created"))

    # -- albums -------------------------------------------------------------

    def _forbidden(self, status: int, permission: str) -> None:
        if status == 403:
            raise ImmichError(f"This API key isn't allowed to do that (it needs the {permission} permission).", status)

    def albums(self) -> list[dict]:
        status, body = self._request("GET", "/albums")
        self._forbidden(status, "album.read")
        return body if isinstance(body, list) else []

    def create_album(self, name: str) -> str:
        status, body = self._request("POST", "/albums", {"albumName": name})
        self._forbidden(status, "album.create")
        if not isinstance(body, dict) or not body.get("id"):
            raise ImmichError(f"Unexpected response creating album: {body!r}", status)
        return body["id"]

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        status, body = self._request("PUT", f"/albums/{quote(album_id)}/assets", {"ids": asset_ids})
        self._forbidden(status, "albumAsset.create")
        for r in body if isinstance(body, list) else []:
            # "duplicate" means it's already in the album, which is fine.
            if not r.get("success") and r.get("error") != "duplicate":
                raise ImmichError(f"Couldn't add to the album: {r.get('error', 'unknown error')}", status)
