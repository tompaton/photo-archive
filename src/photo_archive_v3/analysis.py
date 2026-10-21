"""Deterministic inference from cached facts; skip overrides are the only overrides."""

import json
import re
import sqlite3
from datetime import datetime
from itertools import batched
from pathlib import Path

from .config import relative
from .db import Row, now, rows, settings, update_photo
from .final_names import allocate_final_names, final_name
from .progress import Progress


def filename_match(photo: Row, rules: list[Row]) -> Row:
    for rule in rules:
        match = re.search(rule["pattern"], photo["basename"])
        if match:
            result = dict(rule)
            result["timestamp"] = None
            if rule["timestamp_group"]:
                result["timestamp"] = datetime.strptime(
                    match.group(rule["timestamp_group"]), rule["timestamp_format"]
                ).isoformat()
            return result
    return {}


def matches(raw: Row, conditions: list[Row]) -> bool:
    def condition(c: Row) -> bool:
        value = str(raw.get(c["tag"], ""))
        return (
            value.strip() == c["value"].strip()
            if c["operator"] == "eq"
            else re.search(c["value"], value) is not None
        )

    return all(condition(c) for c in conditions)


def camera_match(
    photo: Row, filename: Row, rules: list[Row]
) -> tuple[str | None, int | None]:
    raw = json.loads(photo["extracted_exif_json"])
    matched = [r for r in rules if matches(raw, r["conditions"])]
    cameras = {r["camera_id"] for r in matched}
    if len(cameras) > 1:
        raise ValueError("Conflicting EXIF camera rules")
    if matched:
        return matched[0]["camera_id"], matched[0]["id"]
    return filename.get("camera_id"), None


def skip_values(photo: Row) -> Row:
    original = photo["overridden_skip_original"]
    final = photo["overridden_skip_final"]
    original = photo["inferred_skip_original"] if original is None else original
    return {
        "result_skip_original": original,
        "result_skip_final": 1
        if original or photo["extracted_kind"] == "video"
        else (photo["inferred_skip_final"] if final is None else final),
    }


def paths(photo: Row, cameras: dict[str, Row]) -> Row:
    camera = cameras.get(photo["result_camera_id"])
    date = (
        datetime.fromisoformat(photo["result_timestamp"])
        if photo["result_timestamp"]
        else None
    )
    values = {
        "camera": photo["result_camera_id"],
        "filename": photo["basename"],
        "stem": Path(photo["basename"]).stem,
        "extension": Path(photo["basename"]).suffix,
        "year": date.strftime("%Y") if date else None,
        "source_parent": Path(photo["source_folder"]).name,
    }
    original = None
    if camera:
        template = camera[
            "video_template" if photo["extracted_kind"] == "video" else "photo_template"
        ]
        if template:
            # A template needing an unknown date must stay pending, not render "None".
            if "{year}" not in template or date:
                original = relative(template.format_map(values))
    final = (
        photo["result_final_path"]
        if photo["status_final_archived_path"]
        else final_name(photo["result_timestamp"])
    )
    return {"result_original_path": original, "result_final_path": final}


def pending(photo: Row) -> str | None:
    reasons = []
    if not photo["extracted_sha256"]:
        reasons.append("Missing source hash")
    if not photo["result_skip_original"] and not photo["result_original_path"]:
        reasons.append("Missing camera or original template/date")
    if not photo["result_skip_final"] and not photo["result_final_path"]:
        reasons.append("Missing capture timestamp")
    return "; ".join(reasons) or None


def timestamp(photo: Row, filename: Row) -> tuple[str | None, str | None]:
    if photo["extracted_timestamp"]:
        return photo["extracted_timestamp"], "exif"
    if filename.get("timestamp"):
        return filename["timestamp"], "filename"
    if photo["extracted_mtime_ns"] is not None:
        date = datetime.fromtimestamp(photo["extracted_mtime_ns"] / 1_000_000_000)
        return date.astimezone().isoformat(), "filesystem"
    return None, None


def infer(
    photo: Row, filenames: list[Row], exif: list[Row], cameras: dict[str, Row]
) -> Row:
    filename = filename_match(photo, filenames)
    camera, exif_id = camera_match(photo, filename, exif)
    date, date_source = timestamp(photo, filename)
    values = {
        "inferred_camera_id": camera,
        "inferred_camera_exif_rule_id": exif_id,
        "inferred_camera_filename_rule_id": filename.get("id")
        if camera and not exif_id
        else None,
        "inferred_timestamp": filename.get("timestamp"),
        "inferred_timestamp_filename_rule_id": filename.get("id")
        if filename.get("timestamp")
        else None,
        "inferred_filename_rule_id": filename.get("id"),
        "inferred_skip_original": filename.get("skip_original") or 0,
        "inferred_skip_final": filename.get("skip_final") or 0,
        "result_camera_id": camera,
        "result_timestamp": date,
        "result_timestamp_source": date_source,
    }
    merged = {**photo, **values}
    values.update(skip_values(merged))
    values.update(paths({**merged, **values}, cameras))
    values["status_pending_reason"] = pending({**photo, **values})
    return values


def analyze(db: sqlite3.Connection, progress: Progress) -> str:
    progress.tick("Loading cached photos and rules…")
    filenames = rows(
        db, "SELECT * FROM filename_rules WHERE enabled ORDER BY priority,id"
    )
    exif = rows(db, "SELECT * FROM exif_rules WHERE enabled ORDER BY id")
    for rule in exif:
        rule["conditions"] = rows(
            db, "SELECT * FROM exif_conditions WHERE rule_id=?", (rule["id"],)
        )
    cameras = {c["id"]: c for c in rows(db, "SELECT * FROM cameras")}
    revision = settings(db)["revision"]
    items = rows(db, "SELECT * FROM photos WHERE status_present")
    processed = 0
    progress.tick(f"Analyze 0/{len(items)}")
    for batch in batched(items, 100):
        with db:
            for photo in batch:
                progress.check_cancelled()
                values: Row = {
                    "status_analyzed_at": now(),
                    "status_analysis_revision": revision,
                }
                try:
                    values.update(infer(photo, filenames, exif, cameras))
                    if photo["status_error_stage"] == "analyze":
                        values.update(
                            status_error_stage=None, status_error_message=None
                        )
                except ValueError as exc:
                    values["status_pending_reason"] = str(exc)
                update_photo(db, photo["id"], values)
        processed += len(batch)
        progress.tick(f"Analyze {processed}/{len(items)}: {batch[-1]['basename']}")
    progress.tick("Assigning unique final filenames…")
    with db:
        allocate_final_names(db)
    return f"Analyzed {len(items)} photos/videos"


def override(
    db: sqlite3.Connection, ids: list[int], output: str, value: bool | None
) -> str:
    if output not in ("original", "final"):
        raise ValueError("Output must be original or final")
    with db:
        for photo_id in ids:
            photo = rows(db, "SELECT * FROM photos WHERE id=?", (photo_id,))[0]
            if output == "final" and photo["extracted_kind"] == "video":
                continue
            key = f"overridden_skip_{output}"
            photo[key] = value
            values = {key: value, **skip_values(photo)}
            values["status_pending_reason"] = pending({**photo, **values})
            update_photo(db, photo_id, values)
        allocate_final_names(db)
    return f"Updated {len(ids)} photos; skipped originals and videos always skip final"
