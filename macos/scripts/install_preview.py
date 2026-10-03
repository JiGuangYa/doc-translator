"""Install a verified local build, preserving a checked backup and rollback path."""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

BUNDLE_ID = "com.jiguang.doctranslator.preview"


def manifest(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = "link:" + os.readlink(path)
        elif path.is_file():
            with path.open("rb") as stream:
                result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        elif path.is_dir():
            result[name] = "directory"
        else:
            raise RuntimeError(f"无法备份特殊文件：{path}")
    return result


def verify_app(path: Path) -> str:
    with (path / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != BUNDLE_ID or info.get("CFBundleExecutable") != "DocTranslatorMac":
        raise RuntimeError(f"不是文档翻译预览应用：{path}")
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(path)], check=True)
    return info["CFBundleShortVersionString"]


def require_stopped(destination: Path) -> None:
    # 'comm' has the full executable path without command-line arguments. Both
    # the native host and its bundled companion must have completed shutdown.
    processes = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, text=True, check=True)
    prefixes = (str(destination) + "/Contents/", str(destination.resolve()) + "/Contents/")
    if any(line.strip().startswith(prefixes) for line in processes.stdout.splitlines()):
        raise RuntimeError("请先在文档翻译中选择“退出”，等待进度保存完成后再安装。")


def copy_tree(source: Path, destination: Path) -> None:
    subprocess.run(["/usr/bin/ditto", str(source), str(destination)], check=True)


def checked_copy(source: Path, destination: Path) -> dict[str, str]:
    before = manifest(source)
    copy_tree(source, destination)
    if manifest(destination) != before or manifest(source) != before:
        raise RuntimeError(f"文件校验失败或复制时文件发生变化：{source}")
    return before


def install_app(source: Path, destination: Path, data: Path, backups: Path) -> dict:
    source, destination = source.absolute(), destination.absolute()
    data, backups = data.absolute(), backups.absolute()
    # Refuse aliases and overlapping trees before any mutation or cleanup.
    if destination.suffix != ".app" or destination.is_symlink():
        raise RuntimeError("安装目标必须是独立的 .app 路径，不能是符号链接。")
    paths = [p.resolve() for p in (source, destination, data, backups)]
    for index, first in enumerate(paths):
        if any(first == second or first in second.parents or second in first.parents for second in paths[index + 1:]):
            raise RuntimeError("应用、数据和备份路径不能相同或互相包含。")
    version = verify_app(source)
    if destination.exists():
        verify_app(destination)
    require_stopped(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (destination.parent / ".doc-translator-preview-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_stopped(destination)
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        backup = Path(tempfile.mkdtemp(prefix=f"{stamp}-pre-{version}-", dir=backups))
        report = {"version": version, "destination": str(destination), "backup": str(backup)}
        if data.exists():
            report["data_manifest"] = checked_copy(data, backup / "data")
        if destination.exists():
            report["app_manifest"] = checked_copy(destination, backup / "app")
        (backup / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        transaction = Path(tempfile.mkdtemp(prefix=".doc-translator-upgrade-", dir=destination.parent))
        incoming, previous = transaction / "incoming.app", transaction / "previous.app"
        committed = False
        try:
            new_manifest = checked_copy(source, incoming)
            verify_app(incoming)
            require_stopped(destination)
            # Recheck data immediately before replacing the bundle. Never write
            # into the live data folder, including during a rollback.
            if data.exists() and manifest(data) != report.get("data_manifest"):
                raise RuntimeError("备份后应用数据发生变化，已保留原版本；请退出应用后重试。")
            if destination.exists():
                destination.rename(previous)
            try:
                incoming.rename(destination)
                verify_app(destination)
                if manifest(destination) != new_manifest:
                    raise RuntimeError("安装后应用文件校验失败")
                report["installed_manifest"] = new_manifest
                (backup / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
                committed = True
            except BaseException:
                if destination.exists():
                    destination.rename(transaction / "failed.app")
                if previous.exists():
                    previous.rename(destination)
                raise
        finally:
            # If rollback itself fails, retain the previous bundle for recovery.
            if committed or not previous.exists():
                shutil.rmtree(transaction)
        return report


def main():
    support = Path.home() / "Library/Application Support"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path.home() / "Applications/文档翻译预览版.app")
    parser.add_argument("--data-dir", type=Path, default=support / "DocTranslatorUnifiedPreview")
    parser.add_argument("--backup-root", type=Path, default=support / "DocTranslatorUnifiedPreview Backups")
    args = parser.parse_args()
    report = install_app(args.app, args.destination, args.data_dir, args.backup_root)
    print(json.dumps({key: value for key, value in report.items() if not key.endswith("manifest")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
