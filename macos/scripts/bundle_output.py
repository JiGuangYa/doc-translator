"""Keep signed macOS build artifacts outside FileProvider-managed source folders."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


APP_NAME = "DocTranslator.app"
ZIP_NAME = "DocTranslator-macOS-arm64.zip"


def running_commands() -> list[str]:
    result = subprocess.run(["ps", "-axo", "pid=,comm="], check=True, capture_output=True, text=True)
    return [parts[1] for line in result.stdout.splitlines() if len(parts := line.strip().split(None, 1)) == 2]


def ensure_not_running(output: Path, *, commands: list[str] | None = None) -> None:
    roots = [(output / name).resolve() for name in (APP_NAME, "DocTranslatorEngine")]
    for command in running_commands() if commands is None else commands:
        executable = Path(command)
        if executable.is_absolute() and any(executable.resolve().is_relative_to(root) for root in roots):
            raise RuntimeError(f"Quit the generated application or test engine before rebuilding: {executable}")


def prepare_output(project: Path, base: Path | None = None) -> Path:
    project = project.resolve()
    key = hashlib.sha256(str(project).encode()).hexdigest()[:20]
    base = base or Path.home() / "Library/Application Support/DocTranslatorBuilds"
    output = base.resolve() / key / "dist"
    link = project / "macos/dist"
    if link.is_symlink():
        if link.resolve() != output:
            raise RuntimeError(f"Existing output link points elsewhere; preserved without changes: {link}")
        ensure_not_running(output)
        output.mkdir(parents=True, exist_ok=True)
        return output
    if link.exists() and not link.is_dir():
        raise RuntimeError(f"Expected a build output directory; preserved without changes: {link}")
    ensure_not_running(link)
    if output.exists():
        if link.exists() or not output.is_dir():
            raise RuntimeError(f"Build output already exists without its workspace link; preserved both locations: {output}")
        # A source cleanup may remove the ignored link. Reconnect the existing
        # artifacts without moving or replacing them.
        ensure_not_running(output)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(output, target_is_directory=True)
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    moved = link.exists()
    if moved:
        shutil.move(str(link), output)
    else:
        output.mkdir()
    try:
        link.symlink_to(output, target_is_directory=True)
    except BaseException:
        if moved:
            shutil.move(str(output), link)
        else:
            output.rmdir()
        raise
    return output


def copy_bundle(source: Path, destination: Path) -> None:
    subprocess.run(["ditto", "--norsrc", "--noextattr", str(source), str(destination)], check=True)


def verify_bundle(app: Path) -> None:
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def archive_bundle(app: Path, archive: Path) -> None:
    subprocess.run(["ditto", "-c", "-k", "--keepParent", "--norsrc", "--noextattr", str(app), str(archive)], check=True)


def publish_bundle(source: Path, output: Path) -> None:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    ensure_not_running(output)
    app, archive = output / APP_NAME, output / ZIP_NAME
    if app.is_symlink() or archive.is_symlink():
        raise RuntimeError("Refusing to replace a symlink at a generated app or ZIP path")
    staging = Path(tempfile.mkdtemp(prefix=".publish-", dir=output))
    clean_staging = True
    try:
        new_app, new_archive = staging / APP_NAME, staging / ZIP_NAME
        previous_app, previous_archive = staging / "previous.app", staging / "previous.zip"
        copy_bundle(source, new_app)
        verify_bundle(new_app)
        archive_bundle(new_app, new_archive)
        # An app may have been launched while the new archive was being built.
        ensure_not_running(output)
        app_published = archive_published = False
        try:
            if app.exists():
                app.rename(previous_app)
            if archive.exists():
                archive.rename(previous_archive)
            new_app.rename(app)
            app_published = True
            new_archive.rename(archive)
            archive_published = True
            verify_bundle(app)
        except BaseException:
            try:
                if app_published:
                    shutil.rmtree(app)
                if archive_published:
                    archive.unlink()
                if previous_app.exists():
                    previous_app.rename(app)
                if previous_archive.exists():
                    previous_archive.rename(archive)
            except BaseException as recovery_error:
                clean_staging = False
                raise RuntimeError(f"Unable to restore previous build automatically. Recovery files preserved at: {staging}") from recovery_error
            raise
    finally:
        if clean_staging:
            shutil.rmtree(staging)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("project", type=Path)
    publish = commands.add_parser("publish")
    publish.add_argument("source", type=Path)
    publish.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            print(prepare_output(args.project))
        else:
            publish_bundle(args.source, args.output)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Build output error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
