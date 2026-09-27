# dcim-to-immich

A touchscreen kiosk for camera uploads. Kids plug a camera in, tap their name, and its
photos and videos go to their [Immich](https://immich.app) library. Then they're
deleted from the camera.

Built for a Canon PowerShot SX20 IS on Ubuntu 26.04. It picks up any USB device that
speaks PTP (almost every camera) or MTP (phones, some cameras); no per-model setup.
Only files under the device's `DCIM/` folder are touched.

## What happens when a camera is plugged in

1. A window opens and reads the camera's model and **serial number**. Each camera is
   identified by serial, so two identical SX20s can belong to different kids.
2. **A camera nobody has claimed yet** shows *"Whose camera is this?"* with a big button
   for each user. Tapping a name assigns the camera to that person from then on. Tapping
   **Cancel** changes nothing, and the camera will ask again next time.
3. It shows how many photos and videos are on the camera, then a progress bar as they
   upload.
4. Each file is copied off the camera, SHA-1 checksummed, and uploaded. **It is deleted
   from the camera only after Immich, asked by that checksum, confirms the user's library
   holds exactly those bytes, and not just in the trash.** Files already there aren't
   uploaded again; they're just removed from the camera. Nothing else on the camera
   is ever deleted.
5. Tapped the wrong name? **Not Bob?** (shown during the upload) stops and asks again.
   Files that already went to Bob stay in Bob's library; the rest go to whoever is picked.
6. When it's finished, the window says **it's safe to unplug the camera**. Unplugging
   closes the window.

Files that fail stay on the camera and are retried next time. After three failures in a
row, or if the user's key is rejected, it stops early. **Stop** finishes the current
file, then says when it's safe to unplug.

## Setup

Installation has two parts: a **system part** that needs root, and a **per-user part**
that doesn't. The kiosk's desktop user doesn't need sudo. The easiest way is to do both
from an admin account:

```sh
sudo ./install.sh --user kiosk     # "kiosk" = the desktop user the kids use
```

Or split it up. An admin runs the system part, then the kiosk user turns it on for
themselves (no sudo):

```sh
sudo ./install.sh                  # admin
./install.sh                       # kiosk user, from a copy of this folder they can read
```

The **system part** installs `python3-gphoto2`, `python3-pyudev` and GTK 3 bindings from
apt. It also installs:

- the udev rule, to `/etc/udev/rules.d/`
- the program, to `/usr/local/lib/dcim-to-immich/`
- a systemd *user* service, to `/etc/systemd/user/`

It doesn't need to know who the kiosk user is. Camera access comes from the udev rule,
which gives it to whoever is logged in at the screen, so no group changes are needed.

The **per-user part** creates a starter config and enables the service, which then
starts with that user's desktop session. `--user` does the same from root while they're
logged out. If they're logged in, it restarts their copy.

To update, re-run the same command.

Then fill in the kiosk user's `~/.config/dcim-to-immich/config.json`. The installer
creates a starter one; from the admin account, `sudo -u kiosk nano ~kiosk/.config/dcim-to-immich/config.json`.

```json
{
  "server": "https://photos.example.com",
  "album": "Camera uploads",
  "users": {
    "Bob": "<Bob's Immich API key>",
    "Sue": "<Sue's Immich API key>"
  },
  "cameras": {}
}
```

- **Keys:** make one per user in Immich (*Account Settings → API Keys*) while signed
  in as that user. It needs `asset.upload`; for albums, also `album.read`,
  `album.create` and `albumAsset.create`.
- **Album** (optional): every upload is added to this album, which is handy for finding
  everything that came off the kiosk.
  - **A name** (exact match): each user gets their own album with that name, made the
    first time it's needed.
  - **An album ID** (the UUID in the album's URL), as a plain string or `{"id": "<uuid>"}`:
    used as-is. This is useful for an album shared with everyone. Give each kid "editor"
    access to it.
  - **One user in a different album:** write their entry as
    `"Sue": {"key": "<Sue's key>", "album": "Sue's camera"}`.
  - **If adding to the album fails**, e.g. a missing permission, files are still uploaded
    and removed from the camera, and the final screen says the album step failed.
- **Cameras:** `cameras` fills itself in as kids tap their names, for example
  `"Canon PowerShot SX20 IS#3C4E…": "Bob"`. Delete an entry to make that camera ask
  again, or change the name to reassign it.
- **Removing a user:** remove them from `users`. Their cameras will ask again.

If the file has a mistake (a typo in the JSON, a misspelled setting), plugging in a camera
shows what's wrong and where, and doesn't touch the camera.

The file is only readable by the kiosk user. Changes take effect on the next plug-in;
no restart is needed.

Logs: `journalctl --user -u dcim-to-immich -f`

Remove it with `sudo ./uninstall.sh --user kiosk`, or `./uninstall.sh` as a user to turn
it off just for them. Either way the config is left in place.

### Why the udev rule?

When the camera is plugged in, the desktop claims it for its own camera browser
(GNOME's gvfs, KDE's Solid). Once it has, nothing else can talk to the camera. The rule
(`udev/72-dcim-to-immich.rules`) matches every PTP/MTP device: interface class
`06/01/01`, or anything libmtp recognises as MTP. It tags the device for this app, hides
it from the desktop, and lets the logged-in user open it. If the desktop grabs one
anyway, the app runs `gio mount -s gphoto2` to release it and retries.

So phones plugged into the kiosk won't show up in the file manager. They get the same
"Whose camera is this?" prompt, and **Cancel** leaves them alone.

## Development

```sh
python3 -m unittest discover -s tests -t .   # tests (fake camera + fake Immich HTTP server)
python3 -m dcim_to_immich --demo             # try the UI: fake camera, users Bob/Sue/Alex (Alex's key is broken)
python3 -m dcim_to_immich -v                 # run the watcher in the foreground
```

Code layout (`dcim_to_immich/`):

- `app.py`: watches udev for cameras coming and going; opens one window and job per camera
- `worker.py`: per-camera job: identify, pick user, upload, verify, delete
- `camera.py`: libgphoto2 access (serial number, DCIM listing, chunked download, delete)
- `immich.py`: Immich API client with streaming uploads (large videos never sit in memory)
- `ui.py`: the touch-sized GTK window
- `config.py`: config file
