from pathlib import Path
import shutil

import pytest

from macos.scripts import bundle_output as bundles


def test_output_migration_preserves_existing_artifacts_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(bundles, "running_commands", lambda: [])
    project, base = tmp_path / "project", tmp_path / "local-builds"
    old = project / "macos/dist"
    (old / bundles.APP_NAME).mkdir(parents=True)
    (old / bundles.APP_NAME / "payload").write_text("previous app")
    (old / bundles.ZIP_NAME).write_bytes(b"previous archive")
    output = bundles.prepare_output(project, base)
    assert old.is_symlink() and old.resolve() == output
    assert (old / bundles.APP_NAME / "payload").read_text() == "previous app"
    assert (output / bundles.ZIP_NAME).read_bytes() == b"previous archive"
    assert bundles.prepare_output(project, base) == output
    old.unlink()
    assert bundles.prepare_output(project, base) == output
    assert (old / bundles.APP_NAME / "payload").read_text() == "previous app"


def test_conflicting_output_directories_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(bundles, "running_commands", lambda: [])
    project, base = tmp_path / "project", tmp_path / "local-builds"
    output = bundles.prepare_output(project, base)
    (output / "keep-local").write_text("local")
    link = project / "macos/dist"
    link.unlink()
    link.mkdir()
    (link / "keep-workspace").write_text("workspace")
    with pytest.raises(RuntimeError, match="preserved both locations"):
        bundles.prepare_output(project, base)
    assert (output / "keep-local").read_text() == "local"
    assert (link / "keep-workspace").read_text() == "workspace"


def test_failed_workspace_link_restores_the_original_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(bundles, "running_commands", lambda: [])
    project = tmp_path / "project"
    old = project / "macos/dist"
    old.mkdir(parents=True)
    (old / "keep.txt").write_text("original")
    monkeypatch.setattr(Path, "symlink_to", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("link failed")))
    with pytest.raises(OSError, match="link failed"):
        bundles.prepare_output(project, tmp_path / "local-builds")
    assert old.is_dir() and not old.is_symlink()
    assert (old / "keep.txt").read_text() == "original"


def test_running_bundle_is_detected_through_workspace_symlink(tmp_path):
    output = tmp_path / "local-builds"
    output.mkdir()
    link = tmp_path / "workspace-dist"
    link.symlink_to(output, target_is_directory=True)
    command = str(link / bundles.APP_NAME / "Contents/MacOS/DocTranslatorMac")
    with pytest.raises(RuntimeError, match="Quit the generated"):
        bundles.ensure_not_running(output, commands=[command])
    # The installed app uses a different directory and does not block a build.
    bundles.ensure_not_running(output, commands=[str(tmp_path / "Applications" / bundles.APP_NAME / "Contents/MacOS/DocTranslatorMac")])


@pytest.fixture
def artifact_pair(tmp_path, monkeypatch):
    source, output = tmp_path / "new.app", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    (source / "payload").write_text("new app")
    (output / bundles.APP_NAME).mkdir()
    (output / bundles.APP_NAME / "payload").write_text("previous app")
    (output / bundles.APP_NAME / "stale").write_text("removed by new build")
    (output / bundles.ZIP_NAME).write_bytes(b"previous archive")
    monkeypatch.setattr(bundles, "running_commands", lambda: [])
    monkeypatch.setattr(bundles, "copy_bundle", lambda src, dest: shutil.copytree(src, dest))
    monkeypatch.setattr(bundles, "verify_bundle", lambda _app: None)
    monkeypatch.setattr(bundles, "archive_bundle", lambda _app, archive: archive.write_bytes(b"new archive"))
    return source, output


def assert_previous_output(output):
    assert (output / bundles.APP_NAME / "payload").read_text() == "previous app"
    assert (output / bundles.ZIP_NAME).read_bytes() == b"previous archive"


def test_failed_zip_creation_keeps_the_previous_build(artifact_pair, monkeypatch):
    source, output = artifact_pair
    monkeypatch.setattr(bundles, "archive_bundle", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        bundles.publish_bundle(source, output)
    assert_previous_output(output)
    assert not list(output.glob(".publish-*"))


def test_failed_final_signature_check_restores_app_and_zip(artifact_pair, monkeypatch):
    source, output = artifact_pair

    def verify(app):
        if app == output / bundles.APP_NAME:
            raise RuntimeError("signature changed")

    monkeypatch.setattr(bundles, "verify_bundle", verify)
    with pytest.raises(RuntimeError, match="signature changed"):
        bundles.publish_bundle(source, output)
    assert_previous_output(output)


def test_failed_rollback_preserves_the_recovery_artifacts(artifact_pair, monkeypatch):
    source, output = artifact_pair
    original_rename = Path.rename

    def rename(path, target):
        if path.name == bundles.ZIP_NAME or path.name == "previous.app":
            raise OSError("rename failed")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(RuntimeError, match="Recovery files preserved"):
        bundles.publish_bundle(source, output)
    recovered = list(output.glob(".publish-*/previous.app/payload"))
    assert len(recovered) == 1 and recovered[0].read_text() == "previous app"
    assert (output / bundles.ZIP_NAME).read_bytes() == b"previous archive"


def test_publishing_replaces_the_whole_bundle_without_stale_files(artifact_pair):
    source, output = artifact_pair
    bundles.publish_bundle(source, output)
    assert (output / bundles.APP_NAME / "payload").read_text() == "new app"
    assert not (output / bundles.APP_NAME / "stale").exists()
    assert (output / bundles.ZIP_NAME).read_bytes() == b"new archive"
    assert not list(output.glob(".publish-*"))
