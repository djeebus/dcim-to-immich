import json
import tempfile
import unittest
from pathlib import Path

from dcim_to_immich.config import Config, ConfigError

ID = "4f8a1c2e-0000-4000-8000-000000000001"


class ConfigTest(unittest.TestCase):
    def load(self, data):
        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return Config.load(path)

    def test_album_forms(self):
        for album, expected in [
            ("Camera uploads", "Camera uploads"),
            ({"name": "Camera uploads"}, "Camera uploads"),
            ({"id": ID}, ID),
            (ID, ID),
        ]:
            with self.subTest(album=album):
                cfg = self.load({"server": "s", "album": album, "users": {"Bob": "k"}})
                self.assertEqual(cfg.album_for("Bob"), expected)

    def test_per_user_album_overrides(self):
        cfg = self.load({
            "album": "Everyone",
            "users": {"Bob": "k1", "Sue": {"key": "k2", "album": {"id": ID}}, "Al": {"key": "k3"}},
        })
        self.assertEqual([cfg.album_for(u) for u in ("Bob", "Sue", "Al")], ["Everyone", ID, "Everyone"])
        self.assertEqual([cfg.api_key(u) for u in ("Bob", "Sue", "Al")], ["k1", "k2", "k3"])

    def test_no_album(self):
        self.assertEqual(self.load({"users": {"Bob": "k"}}).album_for("Bob"), "")

    def test_mistakes_are_explained(self):
        cases = {
            '{"users": {"Bob": "k",}}': "isn't valid JSON",
            '["not", "an", "object"]': "should be a JSON object",
            '{"album": {"uuid": "x"}}': '"album" should be an album name or ID',
            '{"album": ["Camera"]}': '"album" should be an album name or ID',
            '{"users": {"Sue": {"key": "k", "album": 7}}}': 'users → "Sue" → album should be',
            '{"users": {"Sue": {"api_key": "k"}}}': 'unknown setting(s) "api_key"',
            '{"users": {"Sue": {"album": "x"}}}': "needs its API key",
            '{"users": {"Sue": 12345}}': 'users → "Sue" should be an API key',
            '{"users": ["Bob"]}': '"users" should look like',
            '{"server": 1}': '"server" should be',
            '{"cameras": {"cam": {"user": "Bob"}}}': '"cameras" should look like',
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(ConfigError) as cm:
                    self.load(text)
                self.assertIn(message, str(cm.exception))
                self.assertIn("config.json", str(cm.exception))

    def test_missing_file_is_empty(self):
        cfg = Config.load(Path(tempfile.mkdtemp()) / "nope.json")
        self.assertEqual((cfg.server, cfg.users), ("", {}))

    def test_save_keeps_the_album_as_written(self):
        cfg = self.load({"server": "s", "album": {"id": ID}, "users": {"Bob": "k"}})
        cfg.assign("cam", "Bob")
        self.assertEqual(json.loads(cfg.path.read_text())["album"], {"id": ID})


if __name__ == "__main__":
    unittest.main()
