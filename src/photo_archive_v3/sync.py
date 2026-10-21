"""Sync with ordinary copies and per-output checkpoints."""

import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from .config import relative
from .db import Row, photos, settings, update_photo
from .media import check_format, file_hash, generate_final, hash_stream, open_photo
from .progress import Progress


class StaleConfirmation(ValueError):
    """The caller must explicitly confirm using saved stale results."""


def target(root: str | None, name: str | None) -> Path | None:
    if not root or not name:
        return None
    base = Path(root)
    if not base.is_dir():
        raise ValueError(f"NAS root must already exist: {base}")
    path = base / relative(name)
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"Target leaves configured NAS root: {path}")
    return path


def destinations(photo: Row, config: Row) -> tuple[Path | None, Path | None]:
    original = target(config["original_root"], photo["result_original_path"])
    final = (
        None
        if photo["result_skip_final"]
        else target(config["final_root"], photo["result_final_path"])
    )
    if not photo["result_skip_original"] and original is None:
        raise ValueError("Configure the NAS original root")
    if not photo["result_skip_final"] and final is None:
        raise ValueError("Configure the NAS final root")
    return original, final


def preflight(photo: Row, original: Path | None, final: Path | None) -> bool:
    with open_photo(photo) as stream:
        if hash_stream(stream) != photo["extracted_sha256"]:
            raise ValueError(
                "Source no longer matches scanned original; rescan required"
            )
    if final and final.exists() and not final.is_file():
        raise ValueError(f"Final target is not a file: {final}")
    matching = bool(original and original.is_file())
    if matching and file_hash(original) != photo["extracted_sha256"]:
        raise ValueError(f"Original clash: {original}")
    if final and final.exists() and not matching:
        raise ValueError(f"Final clash without matching original: {final}")
    if final and not final.exists():
        check_format(photo)
    return matching


def checkpoint(db: sqlite3.Connection, photo: Row, output: str, path: Path) -> None:
    with db:
        update_photo(db, photo["id"], {f"status_{output}_archived_path": str(path)})


def copy_original(photo: Row, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if photo["zip_index"] < 0:
        Path(photo["source_path"]).copy(destination)
    else:
        with open_photo(photo) as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
    if file_hash(destination) != photo["extracted_sha256"]:
        raise ValueError(f"Copied original hash mismatch: {destination}")


def preview(
    original: Path, final: Path | None, matching: bool, progress: Progress
) -> None:
    action = "Accept matching original" if matching else "Would copy original"
    progress.tick(f"{action}: {original}")
    if final is None:
        progress.tick("Skip final: configured to skip")
    elif final.exists():
        progress.tick(f"Accept existing final: {final}")
    else:
        progress.tick(f"Would generate final: {final}")


def sync_one(
    db: sqlite3.Connection,
    photo: Row,
    config: Row,
    progress: Progress,
    dry_run: bool = False,
) -> None:
    if photo["result_skip_original"]:
        raise ValueError("Original is skipped; final cannot be created")
    original, final = destinations(photo, config)
    progress.tick(f"Checking source and destinations: {photo['basename']}")
    matching = preflight(photo, original, final)
    assert original is not None
    if dry_run:
        preview(original, final, matching, progress)
        return
    if not matching:
        progress.tick(f"Copy original: {original}")
        copy_original(photo, original)
    checkpoint(db, photo, "original", original)
    if final:
        if not final.exists():
            progress.tick(f"Generate final: {final}")
            # Decode before publishing; failed conversion does not leave a partial final.
            with tempfile.TemporaryDirectory(
                prefix="photo-archive-final-"
            ) as directory:
                generated = Path(directory) / "final.jpg"
                generate_final(
                    {**photo, "source_path": str(original), "zip_index": -1}, generated
                )
                final.parent.mkdir(parents=True, exist_ok=True)
                generated.copy(final)
                if file_hash(final) != file_hash(generated):
                    raise ValueError(f"Copied final hash mismatch: {final}")
        checkpoint(db, photo, "final", final)


def sync(
    db: sqlite3.Connection,
    progress: Progress,
    allow_stale: bool = False,
    *,
    dry_run: bool = False,
    retry_errors: bool = False,
) -> str:
    if dry_run:
        progress.tick("Dry run: checking files; no copies or photo status updates")
    retry_ids = []
    if retry_errors:
        retry_ids = [
            p["id"] for p in photos(db, "errors") if p["status_error_stage"] == "sync"
        ]
    # Preview eligibility using the same review rules as the TUI retry, without
    # clearing errors before stale confirmation or during a dry run.
    db.execute("SAVEPOINT sync_retry_preview")
    try:
        for photo_id in retry_ids:
            db.execute(
                "UPDATE photos SET status_error_stage=NULL,status_error_message=NULL WHERE id=?",
                (photo_id,),
            )
        batch = photos(db, "ready")
    finally:
        db.execute("ROLLBACK TO sync_retry_preview")
        db.execute("RELEASE sync_retry_preview")
    stale = sum(p["stale"] for p in batch)
    if stale and not allow_stale:
        raise StaleConfirmation(
            f"{stale} ready photos have out-of-date analysis. Confirm sync using saved results."
        )
    config = settings(db)
    if retry_ids and not dry_run:
        progress.tick(retry(db, retry_ids))
    errors = 0
    for index, photo in enumerate(batch, 1):
        prefix = f"Sync {index}/{len(batch)}"
        item_progress = Progress(
            lambda message, prefix=prefix: progress.emit(f"{prefix}: {message}"),
            progress.cancelled,
        )
        item_progress.tick(f"{photo['source_path']} {photo['zip_member']}")
        try:
            sync_one(db, photo, config, item_progress, dry_run=dry_run)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            errors += 1
            if not dry_run:
                record_error(db, photo, exc)
            item_progress.tick(f"Error {photo['basename']}: {exc}")
    if dry_run:
        return f"Dry run completed: {len(batch) - errors} would archive, {errors} errors; no changes made"
    return f"Sync completed: {len(batch) - errors} archived, {errors} errors"


def record_error(
    db: sqlite3.Connection, photo: Row, exc: Exception, *, stage: str = "sync"
) -> None:
    with db:
        update_photo(
            db,
            photo["id"],
            {"status_error_stage": stage, "status_error_message": str(exc)},
        )


def retry(db: sqlite3.Connection, ids: list[int]) -> str:
    """Clear selected sync errors, retaining paths and successful checkpoints."""
    count = 0
    with db:
        for photo_id in ids:
            count += db.execute(
                "UPDATE photos SET status_error_stage=NULL,status_error_message=NULL "
                "WHERE id=? AND status_error_stage='sync'",
                (photo_id,),
            ).rowcount
    return (
        f"Cleared {count} sync errors; eligible photos will be retried on the next Sync"
    )


def regenerate_one(db: sqlite3.Connection, photo: Row, progress: Progress) -> None:
    original = Path(photo["status_original_archived_path"])
    final = Path(photo["status_final_archived_path"])
    progress.tick(f"Checking archived original: {original}")
    if file_hash(original) != photo["extracted_sha256"]:
        raise ValueError(f"Archived original hash mismatch: {original}")
    progress.tick(f"Regenerate final: {final}")
    final.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a completed image; conversion failures leave the old final intact.
    with tempfile.TemporaryDirectory(
        prefix="photo-archive-final-", dir=final.parent
    ) as directory:
        generated = Path(directory) / "final.jpg"
        generate_final(
            {**photo, "source_path": str(original), "zip_index": -1}, generated
        )
        generated.replace(final)
    with db:
        update_photo(
            db, photo["id"], {"status_error_stage": None, "status_error_message": None}
        )


def regenerate(db: sqlite3.Connection, ids: list[int], progress: Progress) -> str:
    selected = set(ids)
    batch = [
        p
        for p in photos(db)
        if p["id"] in selected
        and not p["result_skip_final"]
        and p["status_original_archived_path"]
        and p["status_final_archived_path"]
        and (p["queue"] == "archived" or p["status_error_stage"] == "regenerate")
    ]
    errors = 0
    for index, photo in enumerate(batch, 1):
        prefix = f"Regenerate {index}/{len(batch)}"
        item_progress = Progress(
            lambda message, prefix=prefix: progress.emit(f"{prefix}: {message}"),
            progress.cancelled,
        )
        try:
            regenerate_one(db, photo, item_progress)
        except (OSError, ValueError, RuntimeError) as exc:
            errors += 1
            record_error(db, photo, exc, stage="regenerate")
            item_progress.tick(f"Error {photo['basename']}: {exc}")
    return f"Regenerated {len(batch) - errors} finals, {errors} errors, {len(selected) - len(batch)} ineligible photos ignored"
