"""Configuration validation and direct SQL writes shared by both interfaces."""

import re
import sqlite3
from pathlib import Path
from string import Formatter

from .db import Row, rows, settings

TABLES = ("sources", "cameras", "exif_rules", "filename_rules")
FIELDS = {"camera", "filename", "stem", "extension", "year", "source_parent"}


def compile_regex(value: str) -> re.Pattern[str]:
    try:
        return re.compile(value)
    except re.PatternError as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc


def relative(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("Destination must be a nonempty relative path without ..")
    return value


def template(value: str) -> None:
    relative(value)
    for _, field, spec, conversion in Formatter().parse(value):
        if field is not None and (field not in FIELDS or spec or conversion):
            raise ValueError(f"Unsupported template field: {field}")


def bump(db: sqlite3.Connection) -> None:
    db.execute("UPDATE settings SET revision=revision+1")


def roots(db: sqlite3.Connection, original: str, final: str, database: Path) -> None:
    paths = [Path(original).absolute(), Path(final).absolute(), database.absolute()]
    for source in rows(db, "SELECT path FROM sources"):
        if any(path.is_relative_to(source["path"]) for path in paths):
            raise ValueError("NAS roots and database must be outside sources")
    if (
        paths[0] == paths[1]
        or paths[0].is_relative_to(paths[1])
        or paths[1].is_relative_to(paths[0])
    ):
        raise ValueError("Original and final roots must be separate directories")
    with db:
        db.execute(
            "UPDATE settings SET original_root=?,final_root=?",
            tuple(map(str, paths[:2])),
        )
        bump(db)


def source(
    db: sqlite3.Connection, path: str, database: Path, enabled: bool = True
) -> None:
    root = Path(path).absolute()
    config = settings(db)
    exclusions = [
        str(database.absolute()),
        config["original_root"],
        config["final_root"],
    ]
    if any(p and Path(p).is_relative_to(root) for p in exclusions):
        raise ValueError("Source must not contain the database or NAS roots")
    with db:
        db.execute(
            "INSERT INTO sources(path,enabled) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET enabled=excluded.enabled",
            (str(root), enabled),
        )


def camera(
    db: sqlite3.Connection, camera_id: str, name: str, photo: str, video: str | None
) -> None:
    if not camera_id.strip() or not name.strip():
        raise ValueError("Camera ID and name are required")
    template(photo)
    if video:
        template(video)
    previous = rows(db, "SELECT * FROM cameras WHERE id=?", (camera_id,))
    with db:
        db.execute(
            "INSERT INTO cameras VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,photo_template=excluded.photo_template,video_template=excluded.video_template",
            (camera_id, name, photo, video or None),
        )
        if previous and (
            previous[0]["photo_template"],
            previous[0]["video_template"],
        ) != (photo, video or None):
            bump(db)


def exif_rule(
    db: sqlite3.Connection,
    camera_id: str,
    conditions: list[tuple[str, str, str]],
    rule_id: int | None = None,
    enabled: bool = True,
) -> int:
    if not conditions:
        raise ValueError("At least one EXIF condition is required")
    for tag, operator, value in conditions:
        if not tag or operator not in ("eq", "regex"):
            raise ValueError("EXIF conditions need a tag and eq/regex operator")
        if operator == "regex":
            compile_regex(value)
    with db:
        cursor = db.execute(
            "INSERT INTO exif_rules(id,camera_id,enabled) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET camera_id=excluded.camera_id,enabled=excluded.enabled",
            (rule_id, camera_id, enabled),
        )
        rule_id = rule_id or cursor.lastrowid
        assert rule_id is not None
        db.execute("DELETE FROM exif_conditions WHERE rule_id=?", (rule_id,))
        db.executemany(
            "INSERT INTO exif_conditions VALUES(?,?,?,?)",
            [(rule_id, *c) for c in conditions],
        )
        bump(db)
    return rule_id


def filename_rule(
    db: sqlite3.Connection, values: Row, rule_id: int | None = None
) -> int:
    pattern = compile_regex(values["pattern"])
    group, format_ = values.get("timestamp_group"), values.get("timestamp_format")
    if bool(group) != bool(format_):
        raise ValueError("Timestamp group and format must both be supplied")
    if group and group not in pattern.groupindex:
        raise ValueError("Timestamp group must name a regex capture group")
    allowed = {
        "pattern",
        "priority",
        "camera_id",
        "timestamp_group",
        "timestamp_format",
        "skip_original",
        "skip_final",
        "enabled",
    }
    if values.keys() - allowed:
        raise ValueError("Unknown filename rule field")
    fields = ["id", *values]
    with db:
        cursor = db.execute(
            f"INSERT INTO filename_rules({','.join(fields)}) VALUES({','.join('?' for _ in fields)}) ON CONFLICT(id) DO UPDATE SET "
            + ",".join(f"{k}=excluded.{k}" for k in values),
            (rule_id, *values.values()),
        )
        bump(db)
    return rule_id or int(cursor.lastrowid or 0)


def statistics(db: sqlite3.Connection, table: str) -> list[Row]:
    columns = {
        "sources": "source_id",
        "cameras": "result_camera_id",
        "exif_rules": "inferred_camera_exif_rule_id",
        "filename_rules": "inferred_filename_rule_id",
    }
    column = columns[table]
    return rows(
        db,
        f"""SELECT c.*,COUNT(p.id) AS matches,
      SUM(p.extracted_kind='photo') AS photo_count,SUM(p.extracted_kind='video') AS video_count,
      MIN(p.result_timestamp) AS min_date,MAX(p.result_timestamp) AS max_date,
      MAX(p.status_scanned_at) AS last_scanned,MAX(p.status_analyzed_at) AS last_analyzed,
      SUM(p.stale) AS stale_count FROM {table} c LEFT JOIN review p ON p.{column}=c.id
      GROUP BY c.id ORDER BY c.id""",
    )
