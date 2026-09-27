import hashlib
import os
import tempfile
import threading
import unittest
from pathlib import Path

from dcim_to_immich.camera import parse_summary
from dcim_to_immich.config import Config
from dcim_to_immich.fakes import FakeCamera, FakeServer
from dcim_to_immich.immich import ExistingAsset, ImmichError
from dcim_to_immich.worker import Job


class RecordingEvents:
    def __init__(self):
        self.log = []
        self.done = threading.Event()
        self.choosing = threading.Event()
        self.summary = None
        self.error = None
        self.on_progress = lambda done, fraction, current: None

    def status(self, text):
        self.log.append(("status", text))

    def choose_user(self, identity, users, error):
        self.log.append(("choose", tuple(users), error))
        self.choosing.set()

    def connected(self, identity, user):
        self.log.append(("connected", user))

    def found(self, photos, videos):
        self.log.append(("found", photos, videos))

    def progress(self, done, total, fraction, current):
        self.on_progress(done, fraction, current)

    def finished(self, summary):
        self.summary = summary
        self.done.set()

    def failed(self, message):
        self.error = message
        self.done.set()


SERIAL = "0123456789abcdef"
CAMERA_ID = f"Canon PowerShot SX20 IS#{SERIAL}"
USERS = {"sue": "sue-key", "Bob": "bob-key", "Alex": "broken-key"}


def sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class JobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = Config(path=self.tmp / "config.json", server="https://immich", users=dict(USERS))
        self.server = FakeServer()

    def camera(self, files, **kw):
        cam = FakeCamera(files, **kw)

        # The invariant under test, checked at the moment of every delete: the file's
        # exact bytes are in the camera owner's library, not trashed.
        def before_delete(item, data):
            owner = self.config.owner(CAMERA_ID if cam.serial == SERIAL else f"Canon PowerShot SX20 IS#{cam.serial}")
            asset = self.server.library(USERS[owner]).get(sha(data))
            self.assertIsNotNone(asset, f"{item.name} deleted but not in {owner}'s library")
            self.assertFalse(asset.trashed, f"{item.name} deleted but only in the trash")

        cam.before_delete = before_delete
        return cam

    def run_job(self, camera, events, before_wait=None):
        job = Job(camera, self.config, events, client_factory=self.server.client, cache_dir=self.tmp / "cache")
        events.job = job
        job.start()
        if before_wait:
            before_wait(job)
        self.assertTrue(events.done.wait(5))
        job.join(5)
        self.assertEqual(os.listdir(self.tmp / "cache") if (self.tmp / "cache").exists() else [], [])
        return job

    def pick(self, events, *names):
        def answer(job):
            for name in names:
                self.assertTrue(events.choosing.wait(5))
                events.choosing.clear()
                job.choose(name) if name else job.cancel()

        return answer

    # -- the happy paths ----------------------------------------------------

    def test_known_camera_uploads_then_deletes(self):
        self.config.cameras[CAMERA_ID] = "Bob"
        cam = self.camera({"IMG_0001.JPG": b"a" * 100, "IMG_0002.JPG": b"b" * 50, "MVI_0003.MOV": b"c" * 300})
        ev = RecordingEvents()
        self.run_job(cam, ev)
        self.assertIsNone(ev.error)
        self.assertNotIn("choose", [e[0] for e in ev.log])
        self.assertIn(("connected", "Bob"), ev.log)
        self.assertEqual(ev.summary.uploaded, 3)
        self.assertEqual(cam.files, {})
        self.assertIn(("found", 2, 1), ev.log)

    def test_duplicate_is_deleted_without_reupload(self):
        self.config.cameras[CAMERA_ID] = "Bob"
        self.server.library("bob-key")[sha(b"dup")] = ExistingAsset("existing", False)
        cam = self.camera({"IMG_0001.JPG": b"dup"})
        ev = RecordingEvents()
        self.run_job(cam, ev)
        self.assertEqual(ev.summary.uploaded, 1)
        self.assertEqual(self.server.uploads, [])
        self.assertEqual(cam.files, {})

    def test_someone_elses_copy_does_not_count(self):
        self.config.cameras[CAMERA_ID] = "Bob"
        self.server.library("sue-key")[sha(b"x")] = ExistingAsset("sues", False)
        cam = self.camera({"IMG_0001.JPG": b"x"})
        ev = RecordingEvents()
        self.run_job(cam, ev)
        self.assertEqual(self.server.uploads, [("bob-key", "IMG_0001.JPG")])
        self.assertEqual(cam.deleted, ["IMG_0001.JPG"])

    # -- never delete what Immich doesn't have -----------------------------

    def test_failed_download_stays_on_camera(self):
        self.config.cameras[CAMERA_ID] = "Bob"
        cam = self.camera({"IMG_0001.JPG": b"a", "IMG_0002.JPG": b"b"})
        cam.fail_download.add("IMG_0001.JPG")
        ev = RecordingEvents()
        self.run_job(cam, ev)
        self.assertEqual(ev.summary.uploaded, 1)
        self.assertEqual(list(cam.files), ["IMG_0001.JPG"])
        self.assertEqual(ev.summary.failed[0][0], "IMG_0001.JPG")

    def test_upload_not_confirmed_stays_on_camera(self):
        # Server says "created" but a checksum lookup can't find it.
        self.config.cameras[CAMERA_ID] = "Bob"
        self.server.lose_uploads = True
        cam = self.camera({"IMG_0001.JPG": b"a"})
        ev = RecordingEvents()
        self.run_job(cam, ev)
        self.assertEqual(cam.deleted, [])
        self.assertIn("didn't confirm", ev.summary.failed[0][1])

    def test_trashed_copy_does_not_count(self):
        self.config.cameras[CAMERA_ID] = "Bob"
        files = {f"IMG_000{i}.JPG": bytes([i]) for i in range(1, 5)}
        for data in list(files.values())[:3]:
            self.server.library("bob-key")[sha(data)] = ExistingAsset("t", True)
        cam = self.camera(files)
        ev = RecordingEvents()
        self.run_job(cam, ev)
        # Three trashed files in a row don't abort the rest.
        self.assertEqual(cam.deleted, ["IMG_0004.JPG"])
        self.assertIsNone(ev.summary.stopped_reason)
        self.assertIn("trash", ev.summary.failed[0][1])

    def test_upload_error_stays_on_camera(self):
        self.config.cameras[CAMERA_ID] = "Bob"
        client = self.server.client

        def failing_client(server, key):
            c = client(server, key)

            def upload(*a, **kw):
                raise ImmichError("Immich returned 500: disk full", 500)

            c.upload = upload
            return c

        cam = self.camera({"IMG_0001.JPG": b"a", "IMG_0002.JPG": b"b", "IMG_0003.JPG": b"c", "IMG_0004.JPG": b"d"})
        ev = RecordingEvents()
        job = Job(cam, self.config, ev, client_factory=failing_client, cache_dir=self.tmp / "cache")
        job.start()
        self.assertTrue(ev.done.wait(5))
        self.assertEqual(cam.deleted, [])
        self.assertEqual(len(ev.summary.failed), 3)  # then it gives up
        self.assertIn("disk full", ev.summary.stopped_reason)

    def test_stop_mid_upload_keeps_that_file(self):
        self.config.cameras[CAMERA_ID] = "Bob"
        cam = self.camera({"IMG_0001.JPG": b"a" * 100, "IMG_0002.JPG": b"b" * 100})
        ev = RecordingEvents()
        ev.on_progress = lambda done, frac, cur: ev.job.cancel() if cur == "IMG_0002.JPG" and frac > 0.6 else None
        self.run_job(cam, ev)
        self.assertEqual(cam.deleted, ["IMG_0001.JPG"])
        self.assertTrue(ev.summary.cancelled)

    # -- choosing whose camera it is ---------------------------------------

    def test_new_camera_picks_user_and_is_remembered(self):
        cam = self.camera({"IMG_0001.JPG": b"a"})
        ev = RecordingEvents()
        # Alex's key is broken: they're asked again, with an error, then pick Sue.
        self.run_job(cam, ev, self.pick(ev, "Alex", "sue"))
        chooses = [e for e in ev.log if e[0] == "choose"]
        self.assertEqual(chooses[0], ("choose", ("Alex", "Bob", "sue"), None))
        self.assertIn("Alex's Immich key didn't work", chooses[1][2])
        self.assertEqual(ev.summary.uploaded, 1)
        saved = Config.load(self.config.path)
        self.assertEqual(saved.cameras, {CAMERA_ID: "sue"})
        self.assertEqual(saved.users, USERS)
        self.assertEqual(oct(os.stat(self.config.path).st_mode & 0o777), "0o600")

    def test_cancel_leaves_camera_untracked(self):
        for _ in range(2):  # asked again on the next plug-in
            cam = self.camera({"IMG_0001.JPG": b"a"})
            ev = RecordingEvents()
            self.run_job(cam, ev, self.pick(ev, None))
            self.assertTrue(ev.summary.cancelled)
            self.assertEqual(list(cam.files), ["IMG_0001.JPG"])
            self.assertFalse(cam.opened)
        self.assertEqual(self.config.cameras, {})
        self.assertFalse(self.config.path.exists())

    def test_same_model_different_serial_is_asked(self):
        self.config.cameras[CAMERA_ID] = "Bob"
        cam = self.camera({"IMG_0001.JPG": b"a"}, serial="ffff")
        ev = RecordingEvents()
        self.run_job(cam, ev, self.pick(ev, None))
        self.assertEqual(ev.log[-1][0], "choose")

    def test_removed_user_means_camera_is_asked_again(self):
        self.config.cameras[CAMERA_ID] = "Former Kid"
        ev = RecordingEvents()
        self.run_job(self.camera({}), ev, self.pick(ev, None))
        self.assertIn("choose", [e[0] for e in ev.log])

    def test_known_camera_with_broken_key_fails(self):
        self.config.cameras[CAMERA_ID] = "Alex"
        cam = self.camera({"IMG_0001.JPG": b"a"})
        ev = RecordingEvents()
        self.run_job(cam, ev)
        self.assertIn("Ask a grown-up", ev.error)
        self.assertEqual(list(cam.files), ["IMG_0001.JPG"])

    def test_not_configured(self):
        self.config.users = {}
        ev = RecordingEvents()
        self.run_job(self.camera({"IMG_0001.JPG": b"a"}), ev)
        self.assertIn("No Immich server or users", ev.error)

    # -- "Not Bob?" --------------------------------------------------------

    def test_not_bob_mid_upload_switches_to_sue(self):
        self.config.cameras[CAMERA_ID] = "Bob"
        files = {f"IMG_000{i}.JPG": bytes([i]) * 100 for i in range(1, 5)}
        cam = self.camera(files)
        ev = RecordingEvents()
        tapped = []

        def on_progress(done, frac, cur):
            # Tap "Not Bob?" halfway through uploading the second file.
            if cur == "IMG_0002.JPG" and frac > 0.7 and not tapped:
                tapped.append(1)
                ev.job.reassign()

        ev.on_progress = on_progress
        self.run_job(cam, ev, self.pick(ev, "sue"))
        # The first file went to Bob before the switch; everything else to Sue,
        # including the one that was mid-upload (it was never deleted as Bob's).
        self.assertEqual(set(self.server.library("bob-key")), {sha(files["IMG_0001.JPG"])})
        self.assertEqual(
            set(self.server.library("sue-key")), {sha(files[n]) for n in ("IMG_0002.JPG", "IMG_0003.JPG", "IMG_0004.JPG")}
        )
        self.assertEqual(cam.files, {})
        self.assertEqual(ev.summary.earlier, {"Bob": 1})
        self.assertEqual(ev.summary.uploaded, 3)
        self.assertEqual(Config.load(self.config.path).cameras, {CAMERA_ID: "sue"})

    def test_not_bob_then_cancel_unassigns(self):
        self.config.cameras[CAMERA_ID] = "Bob"
        cam = self.camera({"IMG_0001.JPG": b"a" * 100, "IMG_0002.JPG": b"b" * 100})
        ev = RecordingEvents()
        ev.on_progress = lambda done, frac, cur: ev.job.reassign() if cur == "IMG_0002.JPG" else None
        self.run_job(cam, ev, self.pick(ev, None))
        self.assertTrue(ev.summary.cancelled)
        self.assertEqual(ev.summary.earlier, {"Bob": 1})
        self.assertEqual(list(cam.files), ["IMG_0002.JPG"])
        self.assertEqual(Config.load(self.config.path).cameras, {})


class SummaryParseTest(unittest.TestCase):
    def test_parse(self):
        text = """Camera summary:
Manufacturer: Canon Inc.
Model: Canon PowerShot SX20 IS
  Version: 1-1.0.2.0
  Serial Number: 3c4e5f60718293a4b5c6d7e8f9001122
Vendor Extension ID: 0xb (1.0)
"""
        self.assertEqual(
            parse_summary(text),
            {
                "Manufacturer": "Canon Inc.",
                "Model": "Canon PowerShot SX20 IS",
                "Serial Number": "3c4e5f60718293a4b5c6d7e8f9001122",
            },
        )


if __name__ == "__main__":
    unittest.main()
