import tempfile
import unittest
import zipfile
from pathlib import Path

from asset_store import close_asset_archives, read_asset_bytes, read_asset_text


class AssetStoreTests(unittest.TestCase):
    def test_reads_source_file_and_release_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            assets = root / "samples"
            assets.mkdir()
            (assets / "value.txt").write_text("исходник", encoding="utf-8")
            self.assertEqual(read_asset_text(assets, "value.txt"), "исходник")

            (assets / "value.txt").unlink()
            with zipfile.ZipFile(root / "samples.zip", "w") as package:
                package.writestr("value.txt", "архив".encode())
            self.assertEqual(read_asset_bytes(assets, "value.txt"), "архив".encode())
            close_asset_archives()

    def test_rejects_parent_path(self):
        with self.assertRaises(ValueError):
            read_asset_bytes(Path("assets"), "../secret")


if __name__ == "__main__":
    unittest.main()
