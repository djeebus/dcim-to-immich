import posixpath
import types
import unittest

from dcim_to_immich.app import is_camera
from dcim_to_immich.camera import CameraError, GPhotoCamera, MediaItem


class FakeGPhoto:
    """Just enough of a gphoto2.Camera to walk a folder tree."""

    def __init__(self, tree):
        self.tree = tree  # path -> list of names; names ending in "/" are folders

    def _entries(self, folder):
        return self.tree.get(folder, [])

    def folder_list_folders(self, folder):
        return [(n.rstrip("/"), None) for n in self._entries(folder) if n.endswith("/")]

    def folder_list_files(self, folder):
        return [(n, None) for n in self._entries(folder) if not n.endswith("/")]

    def file_read(self, folder, name, ftype, offset, view):
        data = self.tree["data"][name]
        chunk = data[offset:offset + len(view)]
        view[:len(chunk)] = chunk
        return len(chunk)

    def file_get_info(self, folder, name):
        return types.SimpleNamespace(file=types.SimpleNamespace(size=10, mtime=hash(posixpath.join(folder, name)) % 1000))


def camera_with(tree):
    cam = GPhotoCamera.__new__(GPhotoCamera)
    cam._gp = types.SimpleNamespace(GPhoto2Error=RuntimeError, GP_FILE_TYPE_NORMAL=1, GP_ERROR_NOT_SUPPORTED=-6)
    cam._cam = FakeGPhoto(tree)
    return cam


class ListMediaTest(unittest.TestCase):
    def test_only_dcim_is_uploaded(self):
        # Shaped like an Android phone over MTP.
        cam = camera_with({
            "/": ["store_00010001/"],
            "/store_00010001": ["DCIM/", "Download/", "Pictures/", "Android/", "stray.jpg"],
            "/store_00010001/DCIM": ["Camera/", "Screenshots/"],
            "/store_00010001/DCIM/Camera": ["IMG_1.jpg", "VID_2.mp4", "notes.txt"],
            "/store_00010001/DCIM/Screenshots": ["shot.png"],
            "/store_00010001/Download": ["meme.jpg"],
            "/store_00010001/Pictures": ["saved.jpg"],
            "/store_00010001/Android": ["data/"],
            "/store_00010001/Android/data": ["cache.jpg"],
        })
        paths = sorted(i.path for i in cam.list_media())
        self.assertEqual(paths, [
            "/store_00010001/DCIM/Camera/IMG_1.jpg",
            "/store_00010001/DCIM/Camera/VID_2.mp4",
            "/store_00010001/DCIM/Screenshots/shot.png",
        ])

    def test_canon_layout(self):
        cam = camera_with({
            "/": ["store_00010001/"],
            "/store_00010001": ["DCIM/", "MISC/"],
            "/store_00010001/DCIM": ["100CANON/", "CANONMSC/"],
            "/store_00010001/DCIM/100CANON": ["IMG_0001.JPG", "MVI_0002.MOV", "MVI_0002.THM"],
            "/store_00010001/DCIM/CANONMSC": ["M0100.CTG"],
        })
        items = {i.name: i for i in cam.list_media()}
        # The .THM sidecar is neither uploaded nor deleted.
        self.assertEqual(sorted(items), ["IMG_0001.JPG", "MVI_0002.MOV"])
        self.assertEqual(items["MVI_0002.MOV"].kind, "video")


class DownloadTest(unittest.TestCase):
    def download(self, stated_size, actual=b"x" * 2500):
        cam = camera_with({"data": {"IMG_1.JPG": actual}})
        out = bytearray()
        item = MediaItem("/store/DCIM/100CANON", "IMG_1.JPG", stated_size, 0, "photo")
        cam.download(item, out.extend, lambda n: None)
        return bytes(out)

    def test_exact_size(self):
        self.assertEqual(self.download(2500), b"x" * 2500)

    def test_rejects_sizes_that_cant_be_trusted(self):
        for size in (0, -1, 0xFFFFFFFF):
            with self.subTest(size=size), self.assertRaises(CameraError):
                self.download(size)

    def test_short_read(self):
        with self.assertRaisesRegex(CameraError, "Only got 2500 of 3000"):
            self.download(3000)


class IsCameraTest(unittest.TestCase):
    def dev(self, **props):
        return types.SimpleNamespace(properties=props)

    def test_matching(self):
        self.assertTrue(is_camera(self.dev(DCIM_TO_IMMICH="1")))
        self.assertTrue(is_camera(self.dev(ID_USB_INTERFACES=":060101:")))
        self.assertTrue(is_camera(self.dev(ID_USB_INTERFACES=":ffff00:", ID_MTP_DEVICE="1")))
        self.assertFalse(is_camera(self.dev(ID_USB_INTERFACES=":080650:")))  # USB stick
        self.assertFalse(is_camera(self.dev()))


if __name__ == "__main__":
    unittest.main()
