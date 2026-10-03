"""Fault injection against disposable app/data trees; no installed app or keys."""

import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_preview as installer


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="doc-translator-installer-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.destination = self.root / "new.app", self.root / "installed.app"
        self.data, self.backups = self.root / "data", self.root / "backups"
        for path, version in ((self.source, "0.2.6"), (self.destination, "0.2.5")):
            (path / "Contents").mkdir(parents=True)
            (path / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": installer.BUNDLE_ID, "CFBundleExecutable": "DocTranslatorMac",
                "CFBundleShortVersionString": version}))
            (path / "Contents/payload").write_text(version)
            (path / "Contents/link").symlink_to("payload")
        self.data.mkdir()
        (self.data / "job.json").write_text('{"saved": true}')
        self.old_manifest = installer.manifest(self.destination)
        self.data_manifest = installer.manifest(self.data)
        self.verify_patch = patch.object(installer, "verify_app", side_effect=lambda path: plistlib.loads(
            (path / "Contents/Info.plist").read_bytes())["CFBundleShortVersionString"])
        self.verify = self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)
        self.stopped_patch = patch.object(installer, "require_stopped")
        self.stopped = self.stopped_patch.start()
        self.addCleanup(self.stopped_patch.stop)
        self.copy_patch = patch.object(installer, "copy_tree", side_effect=lambda a, b: shutil.copytree(a, b, symlinks=True))
        self.copy = self.copy_patch.start()
        self.addCleanup(self.copy_patch.stop)

    def install(self):
        return installer.install_app(self.source, self.destination, self.data, self.backups)

    def assert_old_preserved(self):
        self.assertEqual(installer.manifest(self.destination), self.old_manifest)
        self.assertEqual(installer.manifest(self.data), self.data_manifest)

    def test_success_preserves_verified_backup_and_data(self):
        result = self.install()
        backup = Path(result["backup"])
        self.assertEqual(installer.manifest(self.destination), installer.manifest(self.source))
        self.assertEqual(installer.manifest(backup / "app"), self.old_manifest)
        self.assertEqual(installer.manifest(backup / "data"), self.data_manifest)
        self.assertEqual(installer.manifest(self.data), self.data_manifest)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(self.root.glob(".doc-translator-upgrade-*")))

    def test_running_app_is_refused_before_copy(self):
        self.stopped.side_effect = RuntimeError("still saving")
        with self.assertRaisesRegex(RuntimeError, "still saving"):
            self.install()
        self.copy.assert_not_called()
        self.assert_old_preserved()

    def test_backup_disk_full_preserves_old_app(self):
        self.copy.side_effect = OSError(28, "No space left")
        with self.assertRaises(OSError):
            self.install()
        self.assert_old_preserved()

    def test_bad_install_rolls_back_old_app(self):
        checks = 0

        def reject_installed(path):
            nonlocal checks
            checks += 1
            if checks == 4:
                raise RuntimeError("invalid installed signature")
            return "0.2.6"

        self.verify.side_effect = reject_installed
        with self.assertRaisesRegex(RuntimeError, "invalid installed signature"):
            self.install()
        self.assert_old_preserved()

    def test_failed_final_rename_restores_original(self):
        rename = Path.rename

        def fail_incoming(path, target):
            if path.name == "incoming.app":
                raise OSError(5, "I/O error")
            return rename(path, target)

        with patch.object(Path, "rename", fail_incoming), self.assertRaises(OSError):
            self.install()
        self.assert_old_preserved()

    def test_data_change_after_backup_aborts_install(self):
        def copy_and_change(source, target):
            shutil.copytree(source, target, symlinks=True)
            if target.name == "incoming.app":
                (self.data / "job.json").write_text('{"newer": true}')

        self.copy.side_effect = copy_and_change
        with self.assertRaisesRegex(RuntimeError, "数据发生变化"):
            self.install()
        self.assertEqual(installer.manifest(self.destination), self.old_manifest)
        self.assertEqual((self.data / "job.json").read_text(), '{"newer": true}')

    def test_overlap_and_symlink_destinations_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "互相包含"):
            installer.install_app(self.source, self.source, self.data, self.backups)
        alias = self.root / "alias.app"
        alias.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "符号链接"):
            installer.install_app(self.source, alias, self.data, self.backups)
        self.copy.assert_not_called()

    def test_process_check_includes_companion_with_spaces(self):
        self.stopped_patch.stop()
        result = subprocess.CompletedProcess([], 0, stdout=str(self.destination / "Contents/Resources/Backend/server") + "\n")
        with patch.object(installer.subprocess, "run", return_value=result), self.assertRaisesRegex(RuntimeError, "退出"):
            installer.require_stopped(self.destination)


if __name__ == "__main__":
    unittest.main()
