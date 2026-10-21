"""Incremental discovery and cached observations."""

import sqlite3
import zipfile
from pathlib import Path

from .db import Row, now, rows, update_photo
from .media import kind, observe
from .progress import Progress


def discoveries(root: Path):  # noqa: ANN201
    for path in root.iterdir():
        if path.is_symlink():
            continue
        if path.is_dir():
            yield from discoveries(path)
        elif path.suffix.lower() == ".zip":
            yield from zip_entries(path)
        elif kind(path.name):
            yield identity(path, "", -1, path.stat().st_size)


def identity(path: Path, member: str, index: int, size: int) -> Row:
    name = Path(member or path.name)
    folder = str(path / name.parent) if member else str(path.parent)
    return {
        "source_path": str(path),
        "zip_member": member,
        "zip_index": index,
        "source_folder": folder,
        "basename": name.name,
        "extracted_kind": kind(name.name),
        "extracted_size": size,
        "extracted_mtime_ns": path.stat().st_mtime_ns,
    }


def zip_entries(path: Path):  # noqa: ANN201
    with zipfile.ZipFile(path) as archive:
        for index, info in enumerate(archive.infolist()):
            if not info.is_dir() and kind(info.filename):
                yield identity(path, info.filename, index, info.file_size)


def save(
    db: sqlite3.Connection, item: Row, source_id: int, stamp: str, force: bool
) -> None:
    previous = rows(
        db,
        "SELECT * FROM photos WHERE source_path=? AND zip_index=?",
        (item["source_path"], item["zip_index"]),
    )
    old = previous[0] if previous else {}
    changed = any(
        old.get(key) != item[key]
        for key in ("extracted_size", "extracted_mtime_ns", "zip_member")
    )
    values = {
        **item,
        "source_id": source_id,
        "status_scanned_at": stamp,
        "status_present": 1,
    }
    if changed or force or not old.get("extracted_sha256"):
        values.update(read(item))
        if values.get("extracted_sha256") != old.get("extracted_sha256"):
            values.update(
                status_analyzed_at=None,
                status_analysis_revision=None,
                status_original_archived_path=None,
                status_final_archived_path=None,
                result_original_path=None,
                result_final_path=None,
            )
    with db:
        if old:
            update_photo(db, old["id"], values)
        else:
            keys = ",".join(values)
            db.execute(
                f"INSERT INTO photos({keys}) VALUES({','.join('?' for _ in values)})",
                tuple(values.values()),
            )


def read(item: Row) -> Row:
    try:
        return {
            **observe(item),
            "status_error_stage": None,
            "status_error_message": None,
        }
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        return {
            "extracted_sha256": None,
            "status_error_stage": "scan",
            "status_error_message": str(exc),
        }


def scan(db: sqlite3.Connection, progress: Progress, force: bool = False) -> str:
    count = 0
    failures = []
    seen: set[tuple[str, int]] = set()
    for source in rows(
        db, "SELECT * FROM sources WHERE enabled ORDER BY length(path),id"
    ):
        stamp = now()
        with db:
            db.execute(
                "UPDATE sources SET last_scan_started=? WHERE id=?",
                (stamp, source["id"]),
            )
        try:
            progress.tick(f"Scanning {source['path']}")
            for item in discoveries(Path(source["path"])):
                key = (item["source_path"], item["zip_index"])
                if key in seen:
                    continue
                seen.add(key)
                count += 1
                progress.tick(
                    f"Scan {count}: {item['source_path']} {item['zip_member']}"
                )
                save(db, item, source["id"], stamp, force)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            failures.append(f"{source['path']}: {exc}")
            progress.tick(f"Source error: {failures[-1]}")
            continue
        with db:
            db.execute(
                "UPDATE photos SET status_present=0 WHERE source_id=? AND status_scanned_at != ?",
                (source["id"], stamp),
            )
            db.execute(
                "UPDATE sources SET last_scan_completed=? WHERE id=?",
                (now(), source["id"]),
            )
    return f"Scanned {count} entries; {len(failures)} source errors" + (
        ": " + "; ".join(failures) if failures else ""
    )
