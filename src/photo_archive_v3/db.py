"""SQLite schema and small shared queries."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
 id INTEGER PRIMARY KEY CHECK(id=1), original_root TEXT, final_root TEXT,
 revision INTEGER NOT NULL DEFAULT 1);
INSERT OR IGNORE INTO settings(id) VALUES(1);
CREATE TABLE IF NOT EXISTS sources (
 id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
 last_scan_started TEXT, last_scan_completed TEXT);
CREATE TABLE IF NOT EXISTS cameras (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, photo_template TEXT NOT NULL,
 video_template TEXT);
CREATE TABLE IF NOT EXISTS exif_rules (
 id INTEGER PRIMARY KEY, camera_id TEXT NOT NULL REFERENCES cameras(id),
 enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS exif_conditions (
 rule_id INTEGER NOT NULL REFERENCES exif_rules(id), tag TEXT NOT NULL,
 operator TEXT NOT NULL CHECK(operator IN ('eq','regex')), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS filename_rules (
 id INTEGER PRIMARY KEY, pattern TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100,
 camera_id TEXT REFERENCES cameras(id), timestamp_group TEXT, timestamp_format TEXT,
 skip_original INTEGER, skip_final INTEGER, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS photos (
 id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
 source_path TEXT NOT NULL, zip_member TEXT NOT NULL DEFAULT '',
 zip_index INTEGER NOT NULL DEFAULT -1, source_folder TEXT NOT NULL, basename TEXT NOT NULL,
 extracted_kind TEXT NOT NULL, extracted_size INTEGER, extracted_mtime_ns INTEGER,
 extracted_sha256 TEXT, extracted_exif_json TEXT NOT NULL DEFAULT '{}',
 extracted_make TEXT, extracted_model TEXT, extracted_timestamp TEXT,
 inferred_camera_id TEXT, inferred_camera_exif_rule_id INTEGER,
 inferred_camera_filename_rule_id INTEGER, inferred_timestamp TEXT,
 inferred_timestamp_filename_rule_id INTEGER, inferred_filename_rule_id INTEGER,
 inferred_skip_original INTEGER NOT NULL DEFAULT 0, inferred_skip_final INTEGER NOT NULL DEFAULT 0,
 overridden_skip_original INTEGER, overridden_skip_final INTEGER,
 result_camera_id TEXT, result_timestamp TEXT, result_timestamp_source TEXT,
 result_skip_original INTEGER NOT NULL DEFAULT 0, result_skip_final INTEGER NOT NULL DEFAULT 0,
 result_original_path TEXT, result_final_path TEXT,
 status_scanned_at TEXT, status_analyzed_at TEXT, status_analysis_revision INTEGER,
 status_present INTEGER NOT NULL DEFAULT 1, status_pending_reason TEXT,
 status_error_stage TEXT, status_error_message TEXT,
 status_original_archived_path TEXT, status_final_archived_path TEXT,
 UNIQUE(source_path,zip_index));
CREATE INDEX IF NOT EXISTS photos_hash ON photos(extracted_sha256);
CREATE INDEX IF NOT EXISTS photos_original ON photos(result_original_path);
CREATE INDEX IF NOT EXISTS photos_final ON photos(result_final_path);
CREATE VIEW IF NOT EXISTS photo_states AS
SELECT p.*,
 (result_skip_original AND result_skip_final) AS skipped,
 ((result_skip_original OR status_original_archived_path IS NOT NULL) AND
  (result_skip_final OR status_final_archived_path IS NOT NULL)) AS completed,
 COALESCE(status_analysis_revision != (SELECT revision FROM settings),0) AS stale
FROM photos p;
CREATE VIEW IF NOT EXISTS review AS
SELECT p.*, CASE
 WHEN skipped THEN 'skipped'
 WHEN status_error_message IS NOT NULL THEN 'errors'
 WHEN completed THEN 'archived'
 WHEN EXISTS(SELECT 1 FROM photo_states a WHERE a.id != p.id AND a.completed AND NOT a.skipped
   AND a.extracted_sha256=p.extracted_sha256) THEN 'duplicates'
 WHEN EXISTS(SELECT 1 FROM photo_states a WHERE a.id != p.id AND NOT a.skipped AND
   (a.completed OR a.status_present) AND (
    a.extracted_sha256=p.extracted_sha256 OR
    (NOT a.result_skip_original AND NOT p.result_skip_original AND
     a.result_original_path=p.result_original_path) OR
    (NOT a.result_skip_final AND NOT p.result_skip_final AND
     a.result_final_path=p.result_final_path))) THEN 'duplicates'
 WHEN NOT status_present OR status_analyzed_at IS NULL OR status_pending_reason IS NOT NULL THEN 'pending'
 ELSE 'ready' END AS queue,
 CASE WHEN EXISTS(SELECT 1 FROM photo_states a WHERE a.id != p.id AND a.completed AND NOT a.skipped
   AND a.extracted_sha256=p.extracted_sha256) THEN 'Already archived — no action needed'
 ELSE 'Needs attention' END AS duplicate_note
FROM photo_states p;
"""

Row = dict[str, Any]


def now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def connect(path: Path):  # noqa: ANN201
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    if db.execute("PRAGMA user_version").fetchone()[0] < 1:
        with db:
            db.execute(
                "UPDATE photos SET result_skip_final=1 WHERE result_skip_original"
            )
            db.execute("PRAGMA user_version=1")
    try:
        yield db
    finally:
        db.close()


def rows(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[Row]:
    return [dict(row) for row in db.execute(sql, args)]


def settings(db: sqlite3.Connection) -> Row:
    return rows(db, "SELECT * FROM settings")[0]


def update_photo(db: sqlite3.Connection, photo_id: int, values: Row) -> None:
    columns = ",".join(f"{key}=?" for key in values)
    db.execute(f"UPDATE photos SET {columns} WHERE id=?", (*values.values(), photo_id))


def photos(db: sqlite3.Connection, queue: str | None = None) -> list[Row]:
    if queue:
        return rows(
            db,
            "SELECT * FROM review WHERE queue=? ORDER BY source_folder,basename,id",
            (queue,),
        )
    return rows(db, "SELECT * FROM review ORDER BY source_folder,basename,id")
