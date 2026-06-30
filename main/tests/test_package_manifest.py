import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from rpa.build_package_manifest import collect_entries
from rpa.package_manifest import build_package_manifest, sha256_hex


class PackageManifestTest(unittest.TestCase):
    def test_manifest_records_file_hashes_and_metadata(self):
        manifest = build_package_manifest(
            {"b.txt": b"b", "a.txt": b"a"},
            package_type="rpa-client-script",
            device_id="dev-01",
            signature_status="unsigned",
            generated_at=datetime(2026, 6, 30, 10, 0, 0),
        )

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["package_type"], "rpa-client-script")
        self.assertEqual(manifest["device_id"], "dev-01")
        self.assertEqual([item["path"] for item in manifest["files"]], ["a.txt", "b.txt"])
        self.assertEqual(manifest["files"][0]["sha256"], sha256_hex(b"a"))

    def test_collect_entries_excludes_runtime_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rpa").mkdir()
            (root / "rpa" / "config.json").write_text("{}", encoding="utf-8")
            (root / "logs").mkdir()
            (root / "logs" / "rpa-watchdog.log").write_text("runtime", encoding="utf-8")
            (root / "package_manifest.json").write_text("old", encoding="utf-8")

            entries = collect_entries(root)

        self.assertEqual(set(entries), {"rpa/config.json"})

    def test_exe_build_script_declares_pyinstaller_signing_and_manifest_steps(self):
        script = Path("rpa/build_signed_client.ps1").read_text(encoding="utf-8")

        self.assertIn("PyInstaller", script)
        self.assertIn("signtool.exe", script)
        self.assertIn("rpa.build_package_manifest", script)
        self.assertIn("watchdog_exe.ps1", script)


if __name__ == "__main__":
    unittest.main()
