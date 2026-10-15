"""Create and restore complete, checksummed board backups using only the standard library."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import zipfile

DATABASE = "board.sqlite3"
FORMAT = "blackoutburst-collaboration-board-backup"


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_info(path: Path) -> dict:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    return {"user_version": version,
            "schema_sha256": hashlib.sha256(json.dumps(schema).encode()).hexdigest()}


def create_backup(data_dir: str | Path, output: str | Path) -> dict:
    data = Path(data_dir).resolve(strict=True)
    destination = Path(output).absolute()
    database = data / DATABASE
    if not database.is_file() or database.is_symlink():
        raise ValueError(f"Expected a regular {DATABASE} in the data directory")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Backup output already exists")
    if destination.resolve().is_relative_to(data):
        raise ValueError("Store backups outside the active data directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="board-backup-", dir=destination.parent) as temp:
        staging = Path(temp)
        snapshot = staging / DATABASE
        # Freeze metadata writes while taking the database image and copying files.
        # Core attachment files are immutable and are published before metadata commit.
        locker = sqlite3.connect(database, timeout=30)
        try:
            locker.execute("BEGIN IMMEDIATE")
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
                    target.execute("PRAGMA journal_mode=DELETE")
            with closing(sqlite3.connect(snapshot)) as target:
                referenced = [row[0] for row in target.execute("SELECT id FROM attachments")]
            attachment_dir = data / "attachments"
            if attachment_dir.is_symlink():
                raise ValueError("Attachments directory must not be a symlink")
            for attachment_id in referenced:
                if (not isinstance(attachment_id, str) or not _safe_member(attachment_id)
                        or "/" in attachment_id):
                    raise ValueError("Database contains an invalid attachment ID")
                attachment = attachment_dir / attachment_id
                if attachment.is_symlink() or not attachment.is_file():
                    raise ValueError("Referenced attachment is missing or not a regular file")
                target = staging / "attachments" / attachment_id
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(attachment, target)
        finally:
            locker.rollback()
            locker.close()
        manifest = {"format": FORMAT, "version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "database": DATABASE, "schema": _database_info(snapshot), "files": {}}
        for item in sorted(staging.rglob("*")):
            if item.is_file():
                manifest["files"][item.relative_to(staging).as_posix()] = {
                    "size": item.stat().st_size, "sha256": _hash(item)}
        archive = staging / "backup.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for relative in manifest["files"]:
                bundle.write(staging / relative, relative)
            bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
        # Publish the finished archive atomically and without replacing a concurrent file.
        # Staging lives beside the destination, so the link stays on the same filesystem.
        archive.chmod(0o600)
        os.link(archive, destination)
    return manifest


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return (bool(name) and not path.is_absolute() and "\\" not in name and ":" not in name
            and all(part not in ("", ".", "..") for part in name.split("/")))


def restore_backup(archive: str | Path, data_dir: str | Path) -> dict:
    destination = Path(data_dir).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Restore requires a new, nonexisting data directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        names = [item.filename for item in members]
        if len(names) != len(set(names)) or any(not _safe_member(name) for name in names):
            raise ValueError("Archive contains duplicate or unsafe member paths")
        if "manifest.json" not in names:
            raise ValueError("Archive has no manifest")
        manifest_info = bundle.getinfo("manifest.json")
        if manifest_info.file_size > 4 * 1024 * 1024:
            raise ValueError("Manifest exceeds the size limit")
        manifest = json.loads(bundle.read("manifest.json"))
        if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
                or manifest.get("version") != 1 or manifest.get("database") != DATABASE):
            raise ValueError("Unsupported backup format")
        files = manifest.get("files")
        if not isinstance(files, dict) or DATABASE not in files:
            raise ValueError("Invalid file manifest")
        if set(names) != set(files) | {"manifest.json"}:
            raise ValueError("Archive members do not match the manifest")
        for relative, expected in files.items():
            if (not _safe_member(relative) or
                    (relative != DATABASE and not relative.startswith("attachments/"))):
                raise ValueError("Manifest contains an unsupported path")
            if (not isinstance(expected, dict) or not isinstance(expected.get("size"), int)
                    or expected["size"] < 0 or not isinstance(expected.get("sha256"), str)
                    or len(expected["sha256"]) != 64):
                raise ValueError("Invalid checksum manifest")
            info = bundle.getinfo(relative)
            # Reject directories, symlinks, and special entries before writing anything.
            mode = info.external_attr >> 16
            if info.is_dir() or (mode & 0o170000) not in (0, 0o100000):
                raise ValueError("Archive entries must be regular files")
            if info.file_size != expected["size"]:
                raise ValueError("Archive size differs from manifest")
        with tempfile.TemporaryDirectory(prefix="board-restore-", dir=destination.parent) as temp:
            staging = Path(temp) / "data"
            staging.mkdir(mode=0o700)
            for relative, expected in files.items():
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                written = 0
                with bundle.open(relative) as source, target.open("xb") as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        written += len(chunk)
                        if written > expected["size"]:
                            raise ValueError("Archive entry exceeds declared size")
                        digest.update(chunk)
                        output.write(chunk)
                if written != expected["size"] or digest.hexdigest() != expected["sha256"]:
                    raise ValueError(f"Checksum mismatch: {relative}")
                target.chmod(0o600)
            if _database_info(staging / DATABASE) != manifest.get("schema"):
                raise ValueError("Restored database schema does not match the manifest")
            (staging / "attachments").mkdir(mode=0o700, exist_ok=True)
            # Reserve the target name first; do not replace an existing installation.
            destination.mkdir(mode=0o700)
            try:
                for item in staging.iterdir():
                    shutil.move(str(item), str(destination / item.name))
            except BaseException:
                shutil.rmtree(destination)
                raise
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Back up live SQLite data and immutable attachments")
    create.add_argument("--data-dir", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore", help="Validate and restore to a new data directory")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (create_backup(args.data_dir, args.output) if args.command == "create"
                  else restore_backup(args.input, args.data_dir))
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as error:
        parser.exit(1, f"Backup failed: {error}\n")
    print(json.dumps({"files": len(result["files"]), "schema": result["schema"],
                      "created_at": result["created_at"]}, indent=2))


if __name__ == "__main__":
    main()
