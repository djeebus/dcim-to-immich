"""ImmichClient against a small local HTTP server that mimics the relevant endpoints."""

import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dcim_to_immich.immich import AuthError, ImmichClient, UnavailableError, normalize_server

KEY = "secret"


class Handler(BaseHTTPRequestHandler):
    assets = {}  # id -> sha1 bytes
    trashed = set()  # ids

    def log_message(self, *a):
        pass

    def _json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self):
        if self.headers.get("x-api-key") != KEY:
            self._json(401, {"message": "Invalid API key"})
            return False
        return True

    def do_GET(self):
        if not self._authed():
            return
        if self.path == "/sub/api/users/me":
            return self._json(200, {"name": "Alice", "email": "a@example.com"})
        self._json(404, {"message": "nope"})

    def do_POST(self):
        if not self._authed():
            return
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/sub/api/assets/bulk-upload-check":
            req = json.loads(body)["assets"][0]
            match = [i for i, s in self.assets.items() if s.hex() == req["checksum"]]
            if match:
                result = {"id": req["id"], "action": "reject", "reason": "duplicate", "assetId": match[0],
                          "isTrashed": match[0] in self.trashed}
                return self._json(200, {"results": [result]})
            return self._json(200, {"results": [{"id": req["id"], "action": "accept"}]})
        if self.path == "/sub/api/assets":
            boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
            parts = body.split(b"--" + boundary)
            fields, data = {}, None
            for part in parts[1:-1]:
                head, _, content = part.partition(b"\r\n\r\n")
                content = content[:-2]
                name = head.split(b'name="')[1].split(b'"')[0].decode()
                if name == "assetData":
                    data = content
                else:
                    fields[name] = content.decode()
            assert set(fields) == {"deviceAssetId", "deviceId", "fileCreatedAt", "fileModifiedAt"}, fields
            asset_id = f"asset-{len(self.assets)}"
            self.assets[asset_id] = hashlib.sha1(data).digest()
            return self._json(201, {"id": asset_id, "status": "created"})
        self._json(404, {"message": "nope"})


class ImmichClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_port}/sub/"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_normalize(self):
        self.assertEqual(normalize_server("photos.example.com/api/"), "https://photos.example.com")

    def test_whoami_and_bad_key(self):
        self.assertEqual(ImmichClient(self.url, KEY).whoami(), "Alice")
        with self.assertRaises(AuthError):
            ImmichClient(self.url, "wrong").whoami()

    def test_upload_then_find_by_checksum(self):
        client = ImmichClient(self.url, KEY)
        data = bytes(range(256)) * 9000  # > 2 chunks
        sha = hashlib.sha1(data).hexdigest()
        with tempfile.NamedTemporaryFile(suffix=".JPG") as f:
            f.write(data)
            f.flush()
            self.assertIsNone(client.find_existing(sha))
            seen = []
            result = client.upload(
                f.name,
                filename='IMG "1".JPG',
                device_asset_id="IMG_1.JPG-1",
                device_id="dev",
                created_at="2024-01-01T00:00:00+00:00",
                modified_at="2024-01-01T00:00:00+00:00",
                progress=seen.append,
            )
        self.assertEqual(result.status, "created")
        self.assertEqual(seen[-1], len(data))
        existing = client.find_existing(sha)
        self.assertEqual((existing.asset_id, existing.trashed), (result.asset_id, False))
        self.assertIsNone(client.find_existing(hashlib.sha1(b"other").hexdigest()))
        Handler.trashed.add(result.asset_id)
        self.assertTrue(client.find_existing(sha).trashed)

    def test_unreachable(self):
        with self.assertRaises(UnavailableError):
            ImmichClient("http://127.0.0.1:1", KEY).whoami()


if __name__ == "__main__":
    unittest.main()
