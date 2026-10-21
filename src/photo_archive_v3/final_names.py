"""Allocate planned final names before sync, in source filename order."""

import sqlite3
from datetime import datetime
from pathlib import PurePosixPath

from .db import rows, update_photo


def final_name(timestamp: str | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromisoformat(timestamp).strftime("%Y_%m/%Y_%m_%d-%H_%M_%S.jpg")


def allocate_final_names(db: sqlite3.Connection) -> None:
    items = rows(
        db,
        "SELECT * FROM photos WHERE NOT result_skip_final "
        "AND (status_present OR status_final_archived_path IS NOT NULL) "
        "ORDER BY basename,source_path,zip_index,id",
    )
    # Published names remain fixed, including when a newly scanned filename sorts earlier.
    used = {p["result_final_path"] for p in items if p["status_final_archived_path"]}
    counters: dict[str, int] = {}
    for photo in items:
        if photo["status_final_archived_path"] or not photo["status_analyzed_at"]:
            continue
        base = final_name(photo["result_timestamp"])
        if base is None:
            continue
        path = PurePosixPath(base)
        number = counters.get(base, 0)
        name = base if number == 0 else str(path.with_stem(f"{path.stem}{number}"))
        while name in used:
            number += 1
            name = str(path.with_stem(f"{path.stem}{number}"))
        counters[base] = number + 1
        used.add(name)
        if name != photo["result_final_path"]:
            update_photo(db, photo["id"], {"result_final_path": name})
