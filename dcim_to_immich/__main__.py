"""dcim-to-immich: upload photos and videos from USB cameras to Immich, then clear the camera."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import tempfile
from pathlib import Path

from .config import CONFIG_PATH, Config


def demo(config: Config) -> None:
    """Run the full UI against a fake camera and fake server. Alex's key is broken on purpose."""
    from gi.repository import Gtk

    from .fakes import FakeCamera, FakeServer
    from .ui import CameraWindow, GtkEvents
    from .worker import Job

    files = {f"IMG_{i:04d}.JPG": os.urandom(4000) for i in range(1, 13)}
    files["MVI_0013.MOV"] = os.urandom(20000)
    window = CameraWindow(config)
    window.connect("destroy", Gtk.main_quit)
    window.job = Job(
        FakeCamera(files, delay=0.05),
        config,
        GtkEvents(window),
        client_factory=FakeServer(delay=0.03).client,
        cache_dir=Path(tempfile.mkdtemp(prefix="dcim-demo-")),
    )
    window.job.start()
    Gtk.main()


def main() -> None:
    parser = argparse.ArgumentParser(prog="dcim-to-immich", description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help=f"config file (default: {CONFIG_PATH})")
    parser.add_argument("--demo", action="store_true", help="try the UI with a fake camera and fake Immich server")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    if args.demo:
        tmp = Path(tempfile.mkdtemp(prefix="dcim-demo-"))
        users = {"Bob": "bob-key", "Sue": "sue-key", "Alex": "broken-key"}
        demo(Config(path=tmp / "config.json", server="https://immich.example", users=users, album="Camera uploads"))
        return

    from .app import App

    App(args.config).run()


if __name__ == "__main__":
    main()
